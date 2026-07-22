from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Sum, Q, Prefetch
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models.functions import TruncMonth, TruncYear
import re
from django.db.models import Avg
import json
from django.http import HttpResponseForbidden
from django.utils import timezone
from datetime import timedelta, date
import io
import csv
from django.http import HttpResponse

from .models import Landlord, Property, Unit, Contract, Collection, Tenant, DocumentFolder, DocumentFile, \
    ApprovalDocument, PropertyDocument, SyncLog, BankDeposit

hidden_accounts = ['Landlord (ID: 1)', 'YBN General Portfolio', 'Accounts', 'Output VAT on Sale']


# ==========================================
# THE DYNAMIC OCCUPANCY ENGINE
# ==========================================
def get_property_expanded_stats(property_obj):
    units = property_obj.units.all()
    expanded_dict = {}

    # PASS 1: Initialize sub-units and aggregate ALL contracts touching them
    for u in units:
        if '-' not in u.unit_number:
            continue

        # THE FIX: Strip the colon and everything after it to fuse historical bulk-lease artifacts!
        # Example: "AS001-S01...S05:2952" becomes the clean base name "AS001-S01...S05"
        raw_name = u.unit_number.strip()
        clean_name = raw_name.split(':')[0].strip()

        if clean_name not in expanded_dict:
            expanded_dict[clean_name] = {
                'unit_number': clean_name,
                'is_occupied': False,
                'tenant': None,
                'original_unit': u,
                'base_rent': float(u.base_rent),
                'unit_type': u.unit_type,
                'all_contracts': []
            }

        # Throw all contracts from all historical PACT versions into this unified master pool
        expanded_dict[clean_name]['all_contracts'].extend(list(u.contracts.all()))

    # PASS 2: Determine occupancy strictly by the absolute newest contract
    for sub, data in expanded_dict.items():
        if not data['all_contracts']:
            data['latest_contract'] = None
            continue

        # This will now sort through 2022, 2023, 2024, 2025, and 2026 and pull ONLY the newest one!
        newest_contract = max(data['all_contracts'], key=lambda c: (c.start_date, c.end_date))
        data['latest_contract'] = newest_contract

        if newest_contract.status not in ['Terminated', 'Closed']:
            data['is_occupied'] = True
            data['tenant'] = newest_contract.tenant

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
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant', 'contracts__collections'))
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
            elif 'F' in suffix or 'G' in suffix or 'M' in suffix:
                flats += 1

            # --- THE NEW ADDITION: Sort history for the template ---
            eu['history'] = sorted(eu['all_contracts'], key=lambda c: c.start_date, reverse=True)
            eu['latest_contract'] = eu['history'][0] if eu['history'] else None

        documents = p.documents.all().order_by('-uploaded_at')

        property_data.append({
            'property': p,
            'occupancy_rate': occupancy_rate,
            'exp_units': exp_units,  # <--- Passing ALL units to the template
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
        Prefetch('units', queryset=Unit.objects.prefetch_related('contracts__tenant', 'contracts__collections'))
    )

    property_data = []
    global_stats = {'total_rent': 0, 'paid': 0, 'pdc': 0, 'bounced': 0, 'replaced': 0, 'balance': 0}

    for p in properties:
        exp_units, t_count, _, _ = get_property_expanded_stats(p)

        p_stats = {'total_rent': 0, 'paid': 0, 'pdc': 0, 'bounced': 0, 'replaced': 0, 'balance': 0}

        unique_contracts = set()
        for eu in exp_units:
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                unique_contracts.add(c)

        for c in unique_contracts:
            # We ONLY pull the Net Rent and the cheques here
            p_stats['total_rent'] += float(c.net_rent_amount)

            for col in c.rent_collections:
                amt = float(col.amount)
                if col.status in ['Cleared', 'Posted']:
                    p_stats['paid'] += amt
                elif col.status == 'Pending PDC':
                    p_stats['pdc'] += amt
                elif col.status == 'Bounced':
                    p_stats['bounced'] += amt
                elif col.status in ['Replaced/Partial', 'Replaced']:
                    p_stats['replaced'] += amt

        # --- THE FIX: YOUR EXACT FORMULA ---
        # We calculate the balance for the building AFTER all cheques are summed,
        # guaranteeing the visual cards and pie chart match perfectly.
        calc_balance = p_stats['total_rent'] - (p_stats['paid'] + p_stats['pdc'] + p_stats['replaced'])
        p_stats['balance'] = calc_balance if calc_balance > 0 else 0

        # Add building stats to global pool
        for k in global_stats:
            global_stats[k] += p_stats[k]

        property_data.append({
            'property': p,
            'expanded_units': exp_units,
            'total_count': t_count,
            'stats': p_stats,
            'stats_json': json.dumps(p_stats)
        })

    return render(request, 'realestate/occupancy_status.html', {
        'landlord': landlord,
        'property_data': property_data,
        'global_stats': global_stats,
        'global_stats_json': json.dumps(global_stats)
    })


@login_required(login_url='/login/')
def owner_reports(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user, 'landlord_profile') or request.user.landlord_profile.id != landlord.id:
            return HttpResponseForbidden("<h1>403</h1>")

    prop_filter = request.GET.get('property', '')
    unit_filter = request.GET.get('unit', '').strip()
    tenant_filter = request.GET.get('tenant', '').strip()
    status_filter = request.GET.get('status', '')
    contract_status_filter = request.GET.get('contract_status', '')
    payee_filter = request.GET.get('payee', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    export_format = request.GET.get('export', '')

    reports_data = Collection.objects.filter(contract__unit__property_asset__owner=landlord).select_related(
        'contract__tenant', 'contract__unit__property_asset').order_by('-due_date')

    # Apply Filters
    if prop_filter: reports_data = reports_data.filter(contract__unit__property_asset_id=prop_filter)
    if unit_filter: reports_data = reports_data.filter(contract__unit__unit_number__icontains=unit_filter)
    if tenant_filter: reports_data = reports_data.filter(contract__tenant__full_name__icontains=tenant_filter)
    if status_filter: reports_data = reports_data.filter(status=status_filter)
    if contract_status_filter: reports_data = reports_data.filter(contract__status=contract_status_filter)
    if payee_filter: reports_data = reports_data.filter(payee_company=payee_filter)
    if date_from: reports_data = reports_data.filter(due_date__gte=date_from)
    if date_to: reports_data = reports_data.filter(due_date__lte=date_to)

    # --- CSV EXPORT (Includes Net Rent) ---
    if export_format == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="Comprehensive_Report_{timezone.now().strftime("%Y%m%d")}.csv"'
        writer = csv.writer(response)

        writer.writerow(
            ['Property', 'Unit', 'Tenant Name', 'Payee Company', 'Cheque/Ref Number', 'Cheque Amount (AED)',
             'Contract Net Rent (AED)', 'Due Date', 'Cheque Status', 'Contract Status']
        )
        for row in reports_data:
            writer.writerow([
                row.contract.unit.property_asset.name, row.contract.unit.unit_number, row.contract.tenant.full_name,
                row.payee_company if row.payee_company else "Direct to Owner", row.cheque_number, row.amount,
                row.contract.net_rent_amount,  # <--- Pure Net Rent included
                row.due_date.strftime("%Y-%m-%d") if row.due_date else 'N/A', row.status, row.contract.status
            ])
        return response

        # ==========================================
        # VISUAL ANALYTICS ENGINE
        # ==========================================

        # 1. Revenue Trend Line Chart
    monthly_qs = reports_data.filter(status='Cleared', due_date__isnull=False) \
            .annotate(month=TruncMonth('due_date')).values('month').annotate(total=Sum('amount')).order_by('month')
    chart_months = [m['month'].strftime("%b %Y") for m in monthly_qs if m['month']]
    chart_collections = [float(m['total']) for m in monthly_qs]

        # 2. Multi-Layered Property Revenue (Cleared + Replaced/Partial)
    raw_prop_qs = reports_data.values('contract__unit__property_asset__name', 'status').annotate(
        total=Sum('amount'))
    prop_dict = {}
    for p in raw_prop_qs:
        p_name = p['contract__unit__property_asset__name']
        clean_status = str(p['status']).strip() if p['status'] else 'Unknown'

        if clean_status in ['Cleared', 'Replaced/Partial']:
            if p_name not in prop_dict:
                prop_dict[p_name] = {'Cleared': 0.0, 'Replaced': 0.0}

            if clean_status == 'Cleared':
                prop_dict[p_name]['Cleared'] += float(p['total'] or 0)
            elif clean_status == 'Replaced/Partial':
                prop_dict[p_name]['Replaced'] += float(p['total'] or 0)

    sorted_props = sorted(prop_dict.items(), key=lambda x: x[1]['Cleared'] + x[1]['Replaced'], reverse=True)
    chart_props = [p[0] for p in sorted_props]
    chart_prop_cleared = [p[1]['Cleared'] for p in sorted_props]
    chart_prop_replaced = [p[1]['Replaced'] for p in sorted_props]

        # 3. Status Breakdown Pie
    raw_status_qs = reports_data.values('status').annotate(total=Sum('amount'))
    status_dict = {}
    for s in raw_status_qs:
        clean_status = str(s['status']).strip() if s['status'] else 'Unknown'
        status_dict[clean_status] = status_dict.get(clean_status, 0) + float(s['total'])

    chart_statuses = list(status_dict.keys())
    chart_status_totals = list(status_dict.values())

        # 4. Rent Growth Bar Chart (THE MISSING LINK WAS HERE)
    contract_qs = Contract.objects.filter(unit__property_asset__owner=landlord, start_date__isnull=False)
    if prop_filter: contract_qs = contract_qs.filter(unit__property_asset_id=prop_filter)
    if unit_filter: contract_qs = contract_qs.filter(unit__unit_number__icontains=unit_filter)
    if tenant_filter: contract_qs = contract_qs.filter(tenant__full_name__icontains=tenant_filter)
    if contract_status_filter: contract_qs = contract_qs.filter(status=contract_status_filter)

    rent_trend_qs = contract_qs.annotate(year=TruncYear('start_date')) \
            .values('year').annotate(avg_rent=Avg('net_rent_amount')).order_by('year')
    chart_years = [y['year'].strftime("%Y") for y in rent_trend_qs if y['year']]
    chart_avg_rents = [float(y['avg_rent']) for y in rent_trend_qs]

        # Package it all up for the frontend
    analytics = {
            'months': json.dumps(chart_months), 'collections': json.dumps(chart_collections),
            'props': json.dumps(chart_props),
            'prop_cleared': json.dumps(chart_prop_cleared),
            'prop_replaced': json.dumps(chart_prop_replaced),
            'statuses': json.dumps(chart_statuses), 'status_totals': json.dumps(chart_status_totals),
            'years': json.dumps(chart_years), 'avg_rents': json.dumps(chart_avg_rents),
    }

    # ==========================================
    # DROPDOWN DATA LISTS
    # ==========================================
    properties = Property.objects.filter(owner=landlord)
    units_list = Unit.objects.filter(property_asset__owner=landlord).values_list('unit_number', flat=True).distinct().order_by('unit_number')
    payee_list = Collection.objects.filter(contract__unit__property_asset__owner=landlord).exclude(payee_company__isnull=True).exclude(payee_company='').values_list('payee_company', flat=True).distinct().order_by('payee_company')

    total_amount = reports_data.aggregate(Sum('amount'))['amount__sum'] or 0
    notifications = get_dynamic_notifications(landlord)

    context = {
        'landlord': landlord, 'reports_data': reports_data, 'properties': properties,
        'units_list': units_list, 'payee_list': payee_list,
        'total_amount': total_amount, 'analytics': analytics,
        'filters': {
            'property': int(prop_filter) if prop_filter.isdigit() else '', 'unit': unit_filter,
            'tenant': tenant_filter, 'status': status_filter, 'contract_status': contract_status_filter,
            'payee': payee_filter, 'date_from': date_from, 'date_to': date_to
        },
        'notifications': notifications, 'notif_count': len(notifications)
    }

    return render(request, 'realestate/reports.html', context)

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


@login_required(login_url='/login/')
def owner_cashflow(request, landlord_id):
    landlord = get_object_or_404(Landlord, id=landlord_id)
    if not request.user.is_superuser:
        if not hasattr(request.user, 'landlord_profile') or request.user.landlord_profile.id != landlord.id:
            return HttpResponseForbidden("<h1>403</h1>")

    # 1. READ FILTER INPUTS
    date_from_hand = request.GET.get('date_from_hand', '').strip()
    date_to_hand = request.GET.get('date_to_hand', '').strip()
    date_from_bank = request.GET.get('date_from_bank', '').strip()
    date_to_bank = request.GET.get('date_to_bank', '').strip()
    search_query = request.GET.get('search', '').strip()

    # Default to Year-To-Date on initial page visit only
    if not bool(request.GET):
        today = timezone.now().date()
        date_from_hand = date(today.year, 1, 1).strftime("%Y-%m-%d")
        date_to_hand = today.strftime("%Y-%m-%d")
        date_from_bank = date(today.year, 1, 1).strftime("%Y-%m-%d")
        date_to_bank = today.strftime("%Y-%m-%d")

    # 2. LANDLORD PREFIX MAPPING WITH FALLBACK
    pact_ll_id = str(landlord.pact_landlord_id).strip()
    prefix_map = {'14': ['05'], '15': ['07'], '16': ['08'], '17': ['02'], '18': ['09'], '19': ['06']}
    allowed_prefixes = prefix_map.get(pact_ll_id, [])

    if allowed_prefixes:
        vouchers = BankDeposit.objects.filter(doc_prefix__in=allowed_prefixes)
    else:
        vouchers = BankDeposit.objects.all()

    if search_query:
        vouchers = vouchers.filter(
            Q(narration__icontains=search_query) |
            Q(voucher_no__icontains=search_query) |
            Q(tenant__icontains=search_query) |
            Q(cheque_no__icontains=search_query)
        )

    # 3. APPLY DATE FILTERS ONLY WHEN SPECIFIED
    in_hand_vouchers = vouchers.filter(doc_abbr__in=['CRV', 'CRO']).order_by('-date')
    if date_from_hand:
        in_hand_vouchers = in_hand_vouchers.filter(date__gte=date_from_hand)
    if date_to_hand:
        in_hand_vouchers = in_hand_vouchers.filter(date__lte=date_to_hand)

    in_bank_vouchers = vouchers.filter(doc_abbr__in=['BRV', 'CRT', 'CRVM']).order_by('-date')
    if date_from_bank:
        in_bank_vouchers = in_bank_vouchers.filter(date__gte=date_from_bank)
    if date_to_bank:
        in_bank_vouchers = in_bank_vouchers.filter(date__lte=date_to_bank)

    total_in_hand = in_hand_vouchers.aggregate(Sum('amount'))['amount__sum'] or 0
    total_in_bank = in_bank_vouchers.aggregate(Sum('amount'))['amount__sum'] or 0

    context = {
        'landlord': landlord,
        'in_hand_vouchers': in_hand_vouchers,
        'in_bank_vouchers': in_bank_vouchers,
        'total_in_hand': total_in_hand,
        'total_in_bank': total_in_bank,
        'filters': {
            'date_from_hand': date_from_hand, 'date_to_hand': date_to_hand,
            'date_from_bank': date_from_bank, 'date_to_bank': date_to_bank,
            'search': search_query
        },
        'notifications': get_dynamic_notifications(landlord),
        'notif_count': len(get_dynamic_notifications(landlord))
    }
    return render(request, 'realestate/cashflow.html', context)