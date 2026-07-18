from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q, Prefetch
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.utils import timezone
from datetime import timedelta, date
import io
import csv
from django.http import HttpResponse

from .models import Landlord, Property, Unit, Contract, Collection, Tenant, DocumentFolder, DocumentFile, \
    ApprovalDocument, PropertyDocument, SyncLog

hidden_accounts = ['Landlord (ID: 1)', 'YBN General Portfolio', 'Accounts', 'Output VAT on Sale']


# ==========================================
# THE DYNAMIC OCCUPANCY ENGINE
# ==========================================
def get_property_expanded_stats(property_obj):
    units = property_obj.units.all()
    expanded_dict = {}

    for u in units:
        if '-' not in u.unit_number:
            continue

        # --- THE FIX: ONLY check the newest contract to prevent historical ghosting ---
        latest_contract = u.contracts.order_by('-start_date').first()

        is_occupied = False
        tenant = None

        if latest_contract and latest_contract.status not in ['Terminated', 'Closed']:
            is_occupied = True
            tenant = latest_contract.tenant

        # Explode comma-separated units
        sub_names = [n.strip() for n in u.unit_number.split(',') if n.strip()]
        num_subs = len(sub_names)

        for sub in sub_names:
            apportioned_rent = float(u.base_rent) / num_subs if num_subs > 0 else 0

            if sub in expanded_dict:
                if is_occupied and not expanded_dict[sub]['is_occupied']:
                    expanded_dict[sub]['is_occupied'] = True
                    expanded_dict[sub]['tenant'] = tenant
                    expanded_dict[sub]['original_unit'] = u
                    expanded_dict[sub]['base_rent'] = apportioned_rent
            else:
                expanded_dict[sub] = {
                    'unit_number': sub,
                    'is_occupied': is_occupied,
                    'tenant': tenant,
                    'original_unit': u,
                    'base_rent': apportioned_rent,
                    'unit_type': u.unit_type
                }

    expanded_units = list(expanded_dict.values())
    total_count = len(expanded_units)
    occupied_count = sum(1 for eu in expanded_units if eu['is_occupied'])

    occ_rate = int((occupied_count / total_count) * 100) if total_count > 0 else 0
    expanded_units.sort(key=lambda x: x['unit_number'])

    return expanded_units, total_count, occupied_count, occ_rate


def get_dynamic_notifications(landlord):
    notifs = []
    syncs = list(SyncLog.objects.order_by('-timestamp')[:2])

    if len(syncs) >= 2:
        previous_sync = syncs[1].timestamp
        latest_sync = syncs[0].timestamp
        sync_display = latest_sync.astimezone().strftime("%I:%M %p")
    elif len(syncs) == 1:
        previous_sync = timezone.now() - timedelta(days=1)
        latest_sync = syncs[0].timestamp
        sync_display = latest_sync.astimezone().strftime("%I:%M %p")
    else:
        previous_sync = timezone.now() - timedelta(days=1)
        sync_display = "Recently"

    recent_changes = Collection.objects.filter(
        contract__unit__property_asset__owner=landlord,
        last_updated__gt=previous_sync
    )

    stats = recent_changes.order_by().values('status').annotate(total=Sum('amount'))

    for stat in stats:
        status = stat['status']
        total = stat['total'] or 0
        if total > 0:
            icon = "fa-bell"
            color = "var(--primary-gold)"
            if status == "Cleared":
                icon, color = "fa-check-double", "var(--success)"
            elif status == "Bounced":
                icon, color = "fa-triangle-exclamation", "var(--danger)"
            elif status == "Pending PDC":
                icon, color = "fa-clock", "var(--warning)"
            elif status == "Terminated PDC":
                icon, color = "fa-ban", "#64748b"
            elif "Replaced" in status:
                icon, color = "fa-rotate", "#3b82f6"

            notifs.append({
                'icon': icon, 'color': color, 'title': f'{status} Updates',
                'desc': f'AED {total:,.2f} added to {status} (last sync: {sync_display}).'
            })

    pending_approvals = ApprovalDocument.objects.filter(Q(landlord=landlord) | Q(landlord__isnull=True),
                                                        status='UnApproved').count()
    if pending_approvals > 0:
        notifs.append({'icon': 'fa-file-signature', 'color': 'var(--warning)', 'title': 'Approvals Required',
                       'desc': f"You have {pending_approvals} UnApproved vouchers pending."})

    return notifs


# ==========================================
# PUBLIC & AUTH ROUTES
# ==========================================
def landing_page(request):
    if request.user.is_authenticated:
        if request.user.is_superuser: return redirect('admin_dashboard')
        if hasattr(request.user, 'landlord_profile'): return redirect('owner_dashboard',
                                                                      landlord_id=request.user.landlord_profile.id)
    return render(request, 'realestate/landing_page.html')


def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser: return redirect('admin_dashboard')
        if hasattr(request.user, 'landlord_profile'): return redirect('owner_dashboard',
                                                                      landlord_id=request.user.landlord_profile.id)

    role = request.GET.get('role', 'Executive')

    if request.method == 'POST':
        u = request.POST.get('username', '').strip()
        p = request.POST.get('password', '').strip()

        if not u or not p:
            return render(request, 'realestate/login.html',
                          {'error': 'Please fill out both username and password fields.', 'role': role})

        user = authenticate(request, username=u, password=p)
        if user is not None:
            login(request, user)
            if user.is_superuser: return redirect('admin_dashboard')
            if hasattr(user, 'landlord_profile'): return redirect('owner_dashboard',
                                                                  landlord_id=user.landlord_profile.id)
            return redirect('landing_page')
        else:
            return render(request, 'realestate/login.html',
                          {'error': 'Incorrect username or password. Please try again.', 'role': role})

    return render(request, 'realestate/login.html', {'role': role})


def logout_view(request):
    logout(request)
    return redirect('landing_page')


@login_required(login_url='/login/')
def admin_dashboard(request):
    if not request.user.is_superuser: return HttpResponseForbidden(
        "<h1>403 - Security Violation</h1><p>Admin clearance required.</p>")
    landlords = Landlord.objects.exclude(name__in=hidden_accounts).order_by('name')
    return render(request, 'realestate/portal_home.html', {'landlords': landlords})


# ==========================================
# OWNER DASHBOARD
# ==========================================
@login_required(login_url='/login/')
def owner_dashboard(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user, 'landlord_profile') or request.user.landlord_profile.id != landlord.id:
            return HttpResponseForbidden("<h1>403 - Security Violation</h1>")

    properties = Property.objects.filter(owner=landlord).prefetch_related(
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant'))
    )

    total_units = 0
    occupied_units = 0

    for p in properties:
        _, t_count, o_count, _ = get_property_expanded_stats(p)
        total_units += t_count
        occupied_units += o_count

    occupancy_rate = int((occupied_units / total_units) * 100) if total_units > 0 else 0

    today = timezone.now().date()
    first_of_month = date(today.year, today.month, 1)

    kpi_collections = Collection.objects.filter(contract__unit__property_asset__in=properties,
                                                due_date__gte=first_of_month, due_date__lte=today)
    kpi_breakdown = kpi_collections.order_by().values('status').annotate(total=Sum('amount'))
    kpi_dict = {item['status']: item['total'] or 0 for item in kpi_breakdown}

    kpi_sums = {
        'cleared': kpi_dict.get('Cleared', 0), 'pending': kpi_dict.get('Pending PDC', 0),
        'bounced': kpi_dict.get('Bounced', 0), 'replaced': kpi_dict.get('Replaced', 0),
        'partial': kpi_dict.get('Replaced/Partial', 0), 'terminated': kpi_dict.get('Terminated PDC', 0),
    }

    recent_transactions = Collection.objects.filter(contract__unit__property_asset__in=properties).order_by('-id')[:20]
    notifications = get_dynamic_notifications(landlord)

    context = {
        'landlord': landlord, 'properties_count': properties.count(), 'total_units': total_units,
        'occupancy_rate': occupancy_rate, 'kpi_sums': kpi_sums, 'recent_transactions': recent_transactions,
        'notifications': notifications, 'notif_count': len(notifications)
    }
    return render(request, 'realestate/owner_dashboard.html', context)


@login_required(login_url='/login/')
def owner_properties(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)

    if not request.user.is_superuser:
        if not hasattr(request.user,
                       'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden(
            "<h1>403 - Security Violation</h1>")

    today = timezone.now().date()
    thirty_days = today + timedelta(days=30)
    ninety_days = today + timedelta(days=90)

    # Any contract that physically occupies a flat
    occupying_contracts = Contract.objects.filter(
        unit__property_asset__owner=landlord
    ).exclude(status__in=['Terminated', 'Closed']).select_related('unit__property_asset', 'tenant')

    active_contracts = occupying_contracts.order_by('-start_date')
    renewals = occupying_contracts.filter(end_date__lte=thirty_days).order_by('-end_date')
    expiry_90 = occupying_contracts.filter(end_date__gt=thirty_days, end_date__lte=ninety_days).order_by('end_date')

    # Build Vacant List from exploded units
    properties = Property.objects.filter(owner=landlord).prefetch_related(
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant'))
    )

    vacant_expanded = []
    for p in properties:
        exp_units, _, _, _ = get_property_expanded_stats(p)
        for eu in exp_units:
            if not eu['is_occupied']:
                vacant_expanded.append(eu)

    notifications = get_dynamic_notifications(landlord)

    context = {
        'landlord': landlord, 'active_contracts': active_contracts,
        'renewals': renewals, 'expiry_90': expiry_90,
        'vacant_units': vacant_expanded, 'today': today,
        'notifications': notifications, 'notif_count': len(notifications)
    }
    return render(request, 'realestate/owner_properties.html', context)


@login_required(login_url='/login/')
def owner_vault(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user, 'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden("<h1>403 - Security Violation</h1>")

    transactions = Collection.objects.filter(contract__unit__property_asset__owner=landlord).select_related('contract__tenant', 'contract__unit__property_asset')
    properties = Property.objects.filter(owner=landlord)

    search_query = request.GET.get('search', '')
    property_filter = request.GET.get('property', '')
    status_filter = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    sort_by = request.GET.get('sort', '-id')
    payee_filter = request.GET.get('payee', '') # <--- NEW

    if search_query: transactions = transactions.filter(Q(contract__tenant__full_name__icontains=search_query) | Q(cheque_number__icontains=search_query))
    if property_filter: transactions = transactions.filter(contract__unit__property_asset_id=property_filter)
    if status_filter: transactions = transactions.filter(status=status_filter)
    if payee_filter: transactions = transactions.filter(payee_company=payee_filter) # <--- NEW
    if date_from: transactions = transactions.filter(due_date__gte=date_from)
    if date_to: transactions = transactions.filter(due_date__lte=date_to)

    if sort_by == 'date_asc': transactions = transactions.order_by('due_date')
    elif sort_by == '-due_date': transactions = transactions.order_by('-due_date')
    elif sort_by == 'amount_desc': transactions = transactions.order_by('-amount')
    elif sort_by == 'amount_asc': transactions = transactions.order_by('amount')
    elif sort_by == 'id_asc': transactions = transactions.order_by('id')
    else: transactions = transactions.order_by('-id')

    # Get distinct payee companies for the filter dropdown
    unique_payees = Collection.objects.filter(
        contract__unit__property_asset__owner=landlord
    ).exclude(payee_company__isnull=True).exclude(payee_company='').values_list('payee_company', flat=True).distinct()

    vault_breakdown = transactions.order_by().values('status').annotate(total=Sum('amount'))
    vault_dict = {item['status']: item['total'] or 0 for item in vault_breakdown}

    vault_sums = {
        'cleared': vault_dict.get('Cleared', 0), 'pending': vault_dict.get('Pending PDC', 0),
        'bounced': vault_dict.get('Bounced', 0), 'replaced': vault_dict.get('Replaced', 0),
        'partial': vault_dict.get('Replaced/Partial', 0), 'terminated': vault_dict.get('Terminated PDC', 0),
    }

    notifications = get_dynamic_notifications(landlord)

    context = {
        'landlord': landlord, 'transactions': transactions, 'properties': properties, 'vault_sums': vault_sums,
        'unique_payees': unique_payees, # <--- Pass to template
        'filters': { 'search': search_query, 'property': int(property_filter) if property_filter.isdigit() else '', 'status': status_filter, 'date_from': date_from, 'date_to': date_to, 'sort': sort_by, 'payee': payee_filter },
        'notifications': notifications, 'notif_count': len(notifications)
    }
    return render(request, 'realestate/owner_vault.html', context)


@login_required(login_url='/login/')
def owner_view_properties(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user, 'landlord_profile') or request.user.landlord_profile.id != landlord.id:
            return HttpResponseForbidden("<h1>403 - Security Violation</h1>")

    properties = Property.objects.filter(owner=landlord).prefetch_related(
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant'))
    )
    property_data = []

    for p in properties:
        exp_units, t_count, o_count, occupancy_rate = get_property_expanded_stats(p)

        # --- UNIT CATEGORIZATION LOGIC ---
        flats, parkings, shops = 0, 0, 0
        for eu in exp_units:
            unit_num = eu['unit_number']
            parts = unit_num.split('-')

            # Look at the string segment after the hyphen '-'
            suffix = parts[1].upper() if len(parts) > 1 else unit_num.upper()

            if 'P' in suffix:
                parkings += 1
            elif 'S' in suffix:
                shops += 1
            elif 'F' in suffix or 'G' in suffix or 'M' in suffix:  # <--- Added 'M' right here!
                flats += 1
                # Fetch ALL contracts ordered by newest start_date first
        raw_contracts = Contract.objects.filter(
                    unit__property_asset=p
        ).select_related('tenant', 'unit').order_by('-start_date')

                # --- BULLETPROOF GROUPING LOGIC ---
        grouped_contracts = {}
        for contract in raw_contracts:
            unit_id = contract.unit.id
            if unit_id not in grouped_contracts:
                        # First time seeing this unit (this is the absolute newest contract)
                        # If the newest contract is Closed/Terminated, the unit is VACANT.
                if contract.status in ['Terminated', 'Closed']:
                    grouped_contracts[unit_id] = 'VACANT'
                    continue

                        # Otherwise, it's Occupied! Set up the group.
                grouped_contracts[unit_id] = {
                            'unit': contract.unit,
                            'tenant': contract.tenant,
                            'latest_contract': contract,
                            'history': []
                }

                    # If the unit was flagged as VACANT, skip all historical contracts for this unit
            if grouped_contracts[unit_id] == 'VACANT':
                continue

            grouped_contracts[unit_id]['history'].append(contract)

                # Convert to list and remove the VACANT placeholders
        grouped_contracts_list = sorted(
                    [g for g in grouped_contracts.values() if g != 'VACANT'],
                    key=lambda x: x['unit'].unit_number
        )
        documents = p.documents.all().order_by('-uploaded_at')

        property_data.append({
            'property': p,
            'occupancy_rate': occupancy_rate,
            'grouped_contracts': grouped_contracts_list,  # Passed to template
            'documents': documents,
            'stats': {
                'total': t_count, 'occupied': o_count, 'vacant': t_count - o_count,
                'flats': flats, 'parkings': parkings, 'shops': shops
            }
        })

    notifications = get_dynamic_notifications(landlord)
    return render(request, 'realestate/view_properties.html',
                  {'landlord': landlord, 'property_data': property_data, 'notifications': notifications,
                   'notif_count': len(notifications)})


@login_required(login_url='/login/')
def owner_tenant_details(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user,
                       'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden(
            "<h1>403</h1>")

    search_query = request.GET.get('search', '').strip()
    prop_filter = request.GET.get('property', '')
    unit_filter = request.GET.get('unit', '').strip()
    lease_status = request.GET.get('lease_status', '')

    properties = Property.objects.filter(owner=landlord)
    tenants_query = Tenant.objects.filter(contracts__unit__property_asset__owner=landlord)

    if search_query: tenants_query = tenants_query.filter(
        Q(full_name__icontains=search_query) | Q(phone__icontains=search_query) | Q(
            emirates_id__icontains=search_query))
    if prop_filter: tenants_query = tenants_query.filter(contracts__unit__property_asset_id=prop_filter)
    if unit_filter: tenants_query = tenants_query.filter(contracts__unit__unit_number__icontains=unit_filter)

    if lease_status == 'Active':
        tenants_query = tenants_query.exclude(contracts__status__in=['Terminated', 'Closed'])
    elif lease_status == 'Expired':
        tenants_query = tenants_query.filter(contracts__status__in=['Terminated', 'Closed'])

    tenants = tenants_query.distinct()
    tenant_data = []

    for t in tenants:
        contracts = Contract.objects.filter(tenant=t, unit__property_asset__owner=landlord).select_related(
            'unit__property_asset')
        collections = Collection.objects.filter(contract__in=contracts).order_by('-due_date')

        cleared = collections.filter(status='Cleared').aggregate(Sum('amount'))['amount__sum'] or 0
        pending = collections.filter(status='Pending PDC').aggregate(Sum('amount'))['amount__sum'] or 0
        bounced = collections.filter(status='Bounced').aggregate(Sum('amount'))['amount__sum'] or 0

        tenant_data.append({'tenant': t, 'contracts': contracts,
                            'financials': {'cleared': cleared, 'pending': pending, 'bounced': bounced}})

    notifications = get_dynamic_notifications(landlord)
    return render(request, 'realestate/tenant_details.html', {
        'landlord': landlord, 'tenant_data': tenant_data, 'properties': properties,
        'filters': {'search': search_query, 'property': int(prop_filter) if prop_filter.isdigit() else '',
                    'unit': unit_filter, 'lease_status': lease_status},
        'notifications': notifications, 'notif_count': len(notifications)
    })


@login_required(login_url='/login/')
def owner_occupancy_status(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)

    properties = Property.objects.filter(owner=landlord).prefetch_related(
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant'))
    )

    property_data = []
    for p in properties:
        exp_units, t_count, _, _ = get_property_expanded_stats(p)
        property_data.append({
            'property': p,
            'expanded_units': exp_units,
            'total_count': t_count
        })

    return render(request, 'realestate/occupancy_status.html', {
        'landlord': landlord,
        'property_data': property_data
    })


@login_required(login_url='/login/')
def owner_reports(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user,
                       'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden(
            "<h1>403</h1>")

    prop_filter = request.GET.get('property', '')
    unit_filter = request.GET.get('unit', '').strip()
    tenant_filter = request.GET.get('tenant', '').strip()
    status_filter = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    export_format = request.GET.get('export', '')

    reports_data = Collection.objects.filter(contract__unit__property_asset__owner=landlord).select_related(
        'contract__tenant', 'contract__unit__property_asset').order_by('-due_date')

    if prop_filter: reports_data = reports_data.filter(contract__unit__property_asset_id=prop_filter)
    if unit_filter: reports_data = reports_data.filter(contract__unit__unit_number__icontains=unit_filter)
    if tenant_filter: reports_data = reports_data.filter(contract__tenant__full_name__icontains=tenant_filter)
    if status_filter: reports_data = reports_data.filter(status=status_filter)
    if date_from: reports_data = reports_data.filter(due_date__gte=date_from)
    if date_to: reports_data = reports_data.filter(due_date__lte=date_to)

    if export_format == 'csv':
        response = HttpResponse(content_type='text/csv')
        response[
            'Content-Disposition'] = f'attachment; filename="Comprehensive_Report_{timezone.now().strftime("%Y%m%d")}.csv"'
        writer = csv.writer(response)

        # ADDED 'Payee Company' to headers
        writer.writerow(
            ['Property', 'Unit', 'Tenant Name', 'Payee Company', 'Cheque/Ref Number', 'Amount (AED)', 'Due Date',
             'Status'])

        for row in reports_data:
            # ADDED 'row.payee_company' to the data output
            writer.writerow([
                row.contract.unit.property_asset.name,
                row.contract.unit.unit_number,
                row.contract.tenant.full_name,
                row.payee_company if row.payee_company else "Direct to Owner",
                row.cheque_number,
                row.amount,
                row.due_date.strftime("%Y-%m-%d") if row.due_date else 'N/A',
                row.status
            ])
        return response

    properties = Property.objects.filter(owner=landlord)
    notifications = get_dynamic_notifications(landlord)
    total_amount = reports_data.aggregate(Sum('amount'))['amount__sum'] or 0

    return render(request, 'realestate/reports.html',
                  {'landlord': landlord, 'reports_data': reports_data, 'properties': properties,
                   'total_amount': total_amount,
                   'filters': {'property': int(prop_filter) if prop_filter.isdigit() else '', 'unit': unit_filter,
                               'tenant': tenant_filter, 'status': status_filter, 'date_from': date_from,
                               'date_to': date_to}, 'notifications': notifications, 'notif_count': len(notifications)})


@login_required(login_url='/login/')
def upload_document(request, landlord_id):
    if request.method == 'POST':
        prop_id = request.POST.get('property_id')
        title = request.POST.get('title')
        file = request.FILES.get('document')
        if prop_id and title and file:
            property_obj = get_object_or_404(Property, id=prop_id)
            PropertyDocument.objects.create(property_asset=property_obj, title=title, file=file)
    return redirect('owner_view_properties', landlord_id=landlord_id)


@login_required(login_url='/login/')
def owner_drive(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user,
                       'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden(
            "<h1>403</h1>")

    search_query = request.GET.get('search', '').strip()
    folder_id = request.GET.get('folder')
    current_folder = None

    if search_query:
        folders = DocumentFolder.objects.filter(owner=landlord, name__icontains=search_query)
        files = DocumentFile.objects.filter(owner=landlord, title__icontains=search_query)
    else:
        if folder_id:
            current_folder = get_object_or_404(DocumentFolder, id=folder_id, owner=landlord)
            folders = DocumentFolder.objects.filter(owner=landlord, parent=current_folder).order_by('-created_at')
            files = DocumentFile.objects.filter(owner=landlord, folder=current_folder).order_by('-uploaded_at')
        else:
            folders = DocumentFolder.objects.filter(owner=landlord, parent__isnull=True).order_by('-created_at')
            files = DocumentFile.objects.filter(owner=landlord, folder__isnull=True).order_by('-uploaded_at')

    notifications = get_dynamic_notifications(landlord)
    return render(request, 'realestate/owner_drive.html',
                  {'landlord': landlord, 'folders': folders, 'files': files, 'current_folder': current_folder,
                   'search_query': search_query, 'notifications': notifications, 'notif_count': len(notifications)})


@login_required(login_url='/login/')
def drive_create_folder(request, landlord_id):
    if request.method == 'POST':
        name = request.POST.get('folder_name')
        parent_id = request.POST.get('parent_id')
        landlord = get_object_or_404(Landlord, id=landlord_id)
        if name:
            parent_folder = DocumentFolder.objects.get(id=parent_id) if parent_id else None
            DocumentFolder.objects.create(owner=landlord, name=name, parent=parent_folder)
        if parent_id: return redirect(f"/owner/{landlord_id}/drive/?folder={parent_id}")
    return redirect('owner_drive', landlord_id=landlord_id)


@login_required(login_url='/login/')
def drive_rename_item(request, landlord_id, item_type, item_id):
    if request.method == 'POST':
        new_name = request.POST.get('new_name')
        if new_name:
            if item_type == 'folder':
                folder = get_object_or_404(DocumentFolder, id=item_id, owner_id=landlord_id)
                folder.name = new_name
                folder.save()
            elif item_type == 'file':
                file_obj = get_object_or_404(DocumentFile, id=item_id, owner_id=landlord_id)
                file_obj.title = new_name
                file_obj.save()
    return redirect(request.META.get('HTTP_REFERER', 'owner_drive'))


@login_required(login_url='/login/')
def drive_delete_item(request, landlord_id, item_type, item_id):
    if request.method == 'POST':
        if item_type == 'folder':
            get_object_or_404(DocumentFolder, id=item_id, owner_id=landlord_id).delete()
        elif item_type == 'file':
            get_object_or_404(DocumentFile, id=item_id, owner_id=landlord_id).delete()
    return redirect(request.META.get('HTTP_REFERER', 'owner_drive'))


@login_required(login_url='/login/')
def drive_upload_file(request, landlord_id):
    if request.method == 'POST':
        title = request.POST.get('title')
        file = request.FILES.get('document')
        folder_id = request.POST.get('folder_id')
        landlord = get_object_or_404(Landlord, id=landlord_id)
        if title and file:
            folder = DocumentFolder.objects.get(id=folder_id) if folder_id else None
            DocumentFile.objects.create(owner=landlord, folder=folder, title=title, file=file)
        if folder_id: return redirect(f"/owner/{landlord_id}/drive/?folder={folder_id}")
    return redirect('owner_drive', landlord_id=landlord_id)


@login_required(login_url='/login/')
def owner_approvals(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user,
                       'landlord_profile') or request.user.landlord_profile.id != landlord.id: return HttpResponseForbidden(
            "<h1>403</h1>")

    docs = ApprovalDocument.objects.filter(Q(landlord=landlord) | Q(landlord__isnull=True)).order_by('-doc_date')
    unapproved = docs.filter(status='UnApproved')
    approved = docs.filter(status='Approved')
    rejected = docs.filter(status='Rejected')
    notifications = get_dynamic_notifications(landlord)
    return render(request, 'realestate/approvals.html',
                  {'landlord': landlord, 'unapproved': unapproved, 'approved': approved, 'rejected': rejected,
                   'notifications': notifications, 'notif_count': len(notifications)})


@login_required(login_url='/login/')
def delete_property_document(request, landlord_id, doc_id):
    if request.method == 'POST':
        doc = get_object_or_404(PropertyDocument, id=doc_id, property_asset__owner__id=landlord_id)
        if doc.file: doc.file.delete()
        doc.delete()
    return redirect('owner_view_properties', landlord_id=landlord_id)