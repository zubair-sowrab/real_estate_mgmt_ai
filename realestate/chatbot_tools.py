import difflib
from datetime import datetime, timedelta
import re
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncYear
from django.utils import timezone
from langchain.tools import tool

from .models import (
    ApprovalDocument,
    BankDeposit,
    Collection,
    Contract,
    ContractLedger,
    Landlord,
    OwnerExpense,
    Property,
    Tenant,
    Unit,
    SyncLog,  # NEW
    DocumentFile,  # NEW
    PropertyDocument  # NEW
)
from .views import get_property_expanded_stats


# ========================================================================
# 0. UNIVERSAL HELPERS
# ========================================================================

def get_matched_property(landlord_id: int, search_query: str):
    """A smart fuzzy matcher that finds the correct Property object."""
    if not search_query:
        return None

    properties = Property.objects.filter(owner_id=landlord_id)
    query_clean = search_query.lower().replace('building', '').replace('bldg', '').replace('tower', '').strip()

    for p in properties:
        if query_clean in p.name.lower():
            return p

    query_words = query_clean.replace('al ', '').split()
    for p in properties:
        db_words = p.name.lower().replace('al ', '').replace(' bldg', '').split()
        for qw in query_words:
            if difflib.get_close_matches(qw, db_words, n=1, cutoff=0.6):
                return p

    return None


# ========================================================================
# 1. CORE PROPERTY & TENANT TOOLS
# ========================================================================

@tool
def get_vacant_units(landlord_id: int, property_name: str = None) -> str:
    """Returns a list of truly vacant units based on active contracts."""
    properties = Property.objects.filter(owner_id=landlord_id)

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            properties = properties.filter(id=matched_prop.id)
        else:
            return f"Could not find any building matching '{property_name}'. Please check the spelling."

    if not properties.exists():
        return "You currently have no properties listed."

    results = []

    for p in properties:
        exp_units, _, _, _ = get_property_expanded_stats(p)

        # UI DEDUPLICATOR
        sno_groups = {}
        ghost_unit_ids = set()
        for eu in exp_units:
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                sno = c.pact_costcenter_id
                if sno and sno not in ['0', '1', 'None', '']:
                    if sno not in sno_groups:
                        sno_groups[sno] = []
                    sno_groups[sno].append(eu)

        for sno, eu_list in sno_groups.items():
            if len(eu_list) > 1:
                master_eu = max(eu_list, key=lambda x: len(x['original_unit'].unit_number))
                for eu in eu_list:
                    if eu != master_eu:
                        ghost_unit_ids.add(eu['unit_number'])

        occupied_base_units = set()
        for eu in exp_units:
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                for su in [u.strip() for u in eu['unit_number'].split(',')]:
                    occupied_base_units.add(su)

        all_standalone_units = {eu['unit_number'].strip() for eu in exp_units if ',' not in eu['unit_number']}
        physical_ghost_ids = set()

        for eu in exp_units:
            c = eu.get('latest_contract')
            is_occupied = c and c.status in ['Active', 'Expired (Holdover)']
            sub_units = [u.strip() for u in eu['unit_number'].split(',')]

            if not is_occupied:
                if any(su in occupied_base_units for su in sub_units):
                    physical_ghost_ids.add(eu['unit_number'])
                elif len(sub_units) > 1 and all(su in all_standalone_units for su in sub_units):
                    physical_ghost_ids.add(eu['unit_number'])

        for eu in exp_units:
            u_num = eu['unit_number']
            if u_num in ghost_unit_ids or u_num in physical_ghost_ids or u_num == p.name:
                continue

            c = eu.get('latest_contract')
            is_occupied = c and c.status in ['Active', 'Expired (Holdover)']

            if not is_occupied:
                base_rent = eu.get('original_unit').base_rent if eu.get('original_unit') else 0.0
                results.append(f"- **Unit {u_num}** in {p.name} (Base Rent: AED {float(base_rent):,.2f})")

    if not results:
        if property_name:
            return f"No vacant units found in {properties.first().name} based on active contracts."
        return "You currently have no vacant units across your portfolio."

    return "Here are the truly vacant units:\n" + "\n".join(results)


@tool
def get_tenant_contact_info(landlord_id: int, search_query: str) -> str:
    """Retrieves the phone number, email, and active units of a tenant by searching their name or unit."""
    tenants = Tenant.objects.filter(
        contracts__unit__property_asset__owner_id=landlord_id
    ).distinct()

    tenants = tenants.filter(
        Q(full_name__icontains=search_query) |
        Q(phone__icontains=search_query) |
        Q(contracts__unit__unit_number__icontains=search_query)
    )

    if not tenants.exists():
        return f"Could not find any tenant matching '{search_query}'."

    results = []
    for t in tenants:
        active_units = [c.unit.unit_number for c in t.contracts.filter(status__in=['Active', 'Expired (Holdover)'])]
        unit_str = ", ".join(active_units) if active_units else "No active units"
        results.append(f"Name: {t.full_name} | Phone: {t.phone} | Email: {t.email or 'N/A'} | Unit(s): {unit_str}")

    return "\n".join(results)


@tool
def get_contract_balance(landlord_id: int, unit_number: str, property_name: str = None) -> str:
    """Retrieves the financial status of a contract for a specific unit."""
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        status__in=['Active', 'Expired (Holdover)']
    ).select_related('tenant', 'unit')

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contracts = contracts.filter(unit__property_asset_id=matched_prop.id)
        else:
            return f"Could not find any building matching '{property_name}'."

    clean_unit = unit_number.lower().replace('flat', '').replace('shop', '').replace(' ', '')
    contracts = contracts.filter(unit__unit_number__icontains=clean_unit)

    if not contracts.exists():
        return f"No active contract found for unit '{unit_number}'{' in ' + property_name if property_name else ''}."

    contract = contracts.order_by('-start_date').first()
    return (
        f"Tenant: {contract.tenant.full_name}\n"
        f"Unit: {contract.unit.unit_number}\n"
        f"Comprehensive Rent: AED {contract.comprehensive_rent_amount}\n"
        f"Cleared Paid: AED {contract.cleared_paid}\n"
        f"Remaining Strict Balance: AED {contract.strict_balance}\n"
        f"Status: {contract.status}"
    )


@tool
def get_unit_rent_history(landlord_id: int, unit_number: str, property_name: str = None) -> str:
    """Retrieves the complete historical list of contracts and rent amounts for a specific unit."""
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id
    )

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contracts = contracts.filter(unit__property_asset_id=matched_prop.id)
        else:
            return f"Could not find any building matching '{property_name}'."

    clean_unit = unit_number.lower().replace('flat', '').replace('shop', '').replace(' ', '')

    contracts = contracts.filter(
        unit__unit_number__icontains=clean_unit
    ).select_related('tenant', 'unit', 'unit__property_asset').order_by('-start_date')

    if not contracts.exists():
        return f"No contract history found for unit '{unit_number}'{' in ' + property_name if property_name else ''}."

    unit_ref = contracts.first().unit
    results = [f"**Rent History for Unit {unit_ref.unit_number} ({unit_ref.property_asset.name}):**\n"]

    for c in contracts:
        start = c.start_date.strftime('%d %b %Y') if c.start_date else 'Unknown'
        end = c.end_date.strftime('%d %b %Y') if c.end_date else 'Unknown'
        rent = float(c.comprehensive_rent_amount) if c.comprehensive_rent_amount else 0.0

        results.append(
            f"- **{start} to {end}**: AED {rent:,.2f} | Tenant: {c.tenant.full_name} | Status: {c.status}"
        )

    return "\n".join(results)


# ========================================================================
# 2. ANALYTICS & HISTORICAL REPORTS
# ========================================================================

@tool
def get_rent_growth_report(landlord_id: int, property_name: str = None) -> str:
    """Calculates historical yearly average rent growth across properties or for a specific building."""
    contract_qs = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        start_date__isnull=False
    )

    target_name = "Portfolio-wide"
    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contract_qs = contract_qs.filter(unit__property_asset_id=matched_prop.id)
            target_name = matched_prop.name
        else:
            return f"Could not find any building matching '{property_name}'."

    growth_qs = (
        contract_qs.annotate(year=TruncYear('start_date'))
        .values('year')
        .annotate(avg_rent=Avg('comprehensive_rent_amount'), total_leases=Count('id'))
        .order_by('year')
    )

    if not growth_qs.exists():
        return f"No historical contract data found for {target_name}."

    lines = [f"**Historical Rent Growth Report ({target_name}):**"]
    for row in growth_qs:
        year_str = row['year'].strftime('%Y') if row['year'] else 'Unknown'
        avg_rent = float(row['avg_rent'] or 0.0)
        lines.append(f"- **{year_str}**: Average Rent = AED {avg_rent:,.2f} ({row['total_leases']} contracts)")

    return "\n".join(lines)


@tool
def compare_properties(landlord_id: int, property_name_a: str, property_name_b: str) -> str:
    """Compares two properties side-by-side on total units, occupancy rate, total billed rent, and maintenance expenses."""
    prop_a = get_matched_property(landlord_id, property_name_a)
    prop_b = get_matched_property(landlord_id, property_name_b)

    if not prop_a or not prop_b:
        missing = [name for name, prop in zip([property_name_a, property_name_b], [prop_a, prop_b]) if not prop]
        return f"Could not find exact matching building(s): {', '.join(missing)}."

    def calculate_metrics(prop):
        _, total_count, occupied_count, occ_rate = get_property_expanded_stats(prop)
        billed = ContractLedger.objects.filter(contract__unit__property_asset=prop, is_debit=True).aggregate(
            total=Sum('amount'))['total'] or 0.0
        expenses = OwnerExpense.objects.filter(landlord_id=landlord_id, tower_name__icontains=prop.name).aggregate(
            total=Sum('amount'))['total'] or 0.0

        return {'total_units': total_count, 'occupied': occupied_count, 'vacant': total_count - occupied_count,
                'occ_rate': occ_rate, 'total_billed': float(billed), 'expenses': float(expenses)}

    data_a, data_b = calculate_metrics(prop_a), calculate_metrics(prop_b)

    return (
        f"**Comparison: {prop_a.name} vs {prop_b.name}**\n\n"
        f"**{prop_a.name}**:\n"
        f"- Total Units: {data_a['total_units']} (Occupied: {data_a['occupied']}, Vacant: {data_a['vacant']})\n"
        f"- Occupancy Rate: {data_a['occ_rate']}%\n"
        f"- Billed Rent: AED {data_a['total_billed']:,.2f} | Expenses: AED {data_a['expenses']:,.2f}\n\n"
        f"**{prop_b.name}**:\n"
        f"- Total Units: {data_b['total_units']} (Occupied: {data_b['occupied']}, Vacant: {data_b['vacant']})\n"
        f"- Occupancy Rate: {data_b['occ_rate']}%\n"
        f"- Billed Rent: AED {data_b['total_billed']:,.2f} | Expenses: AED {data_b['expenses']:,.2f}"
    )


# ========================================================================
# 3. TENANT PERFORMANCE & HEALTH EVALUATION
# ========================================================================

@tool
def get_tenant_performance(landlord_id: int, search_query: str) -> str:
    """Evaluates tenant risk score, payment timeliness, bounced cheques, contract expiry, and maintenance cost ratio."""

    # 🚀 SAFETY SHIELD: Prevents Context Window Crashes
    if not search_query or len(search_query.strip()) < 2:
        return "Please provide a specific tenant name or unit number to check performance. I cannot evaluate the entire database at once."

    today = timezone.now().date()
    contracts = Contract.objects.filter(unit__property_asset__owner_id=landlord_id).filter(
        Q(tenant__full_name__icontains=search_query) | Q(unit__unit_number__icontains=search_query)
    ).select_related('tenant', 'unit', 'unit__property_asset')

    if not contracts.exists():
        return f"No tenant or contract records found matching '{search_query}'."

    results = []
    for c in contracts:
        total_rent = float(c.comprehensive_rent_amount) or float(c.unit.base_rent)
        cleared = float(c.cleared_paid)
        pdc = float(sum(col.amount for col in c.collections.filter(status='Pending PDC')))
        bounced_count = c.collections.filter(status='Bounced').count()

        time_pct, days_left, days_expired = 0, 0, 0
        if c.start_date and c.end_date:
            total_days = (c.end_date - c.start_date).days or 1
            passed_days = (today - c.start_date).days
            time_pct = min(100, max(0, int((passed_days / total_days) * 100)))
            days_left = (c.end_date - today).days
            if days_left < 0:
                days_expired = abs(days_left)
                days_left = 0

        cleared_pct = min(100, int((cleared / total_rent) * 100)) if total_rent > 0 else 0
        exp_agg = OwnerExpense.objects.filter(
            landlord_id=landlord_id,
            unit_name=c.unit.unit_number,
            tower_name=c.unit.property_asset.name,
            date__gte=c.start_date if c.start_date else datetime(2000, 1, 1).date()
        ).aggregate(total=Sum('amount'))
        expenses = float(exp_agg['total'] or 0)

        issues = []
        if bounced_count > 0: issues.append(f"{bounced_count} Bounced Cheque(s) recorded")
        if cleared_pct < (time_pct - 15) and (cleared + pdc) < total_rent * 0.95: issues.append(
            f"Rent payments ({cleared_pct}%) lag behind elapsed time ({time_pct}%)")
        if days_expired > 0: issues.append(f"Contract expired {days_expired} days ago")
        if expenses > (total_rent * 0.15) and expenses > 0: issues.append(
            f"High maintenance costs (AED {expenses:,.0f})")

        grade = "At Risk" if (bounced_count > 0 or days_expired > 0 or (
                cleared_pct < (time_pct - 25) and (cleared + pdc) < total_rent * 0.95)) else (
            "Excellent" if not issues and (cleared_pct >= time_pct or (cleared + pdc) >= total_rent * 0.95) else (
                "Warning" if issues else "Good"))

        results.append(
            f"**Tenant:** {c.tenant.full_name} | **Unit:** {c.unit.unit_number} ({c.unit.property_asset.name})\n"
            f"- **Performance Grade:** {grade}\n"
            f"- **Total Rent:** AED {total_rent:,.2f} (Cleared: AED {cleared:,.2f} [{cleared_pct}%], Pending PDC: AED {pdc:,.2f})\n"
            f"- **Contract Status:** {c.status} (Elapsed: {time_pct}%, Days Left: {days_left})\n"
            f"- **Maintenance Spent:** AED {expenses:,.2f}\n"
            f"- **Risk Flags:** {', '.join(issues) if issues else 'None'}"
        )
    return "\n\n---\n\n".join(results)


@tool
def get_bounced_cheques_report(landlord_id: int) -> str:
    """Retrieves a concise list of all tenants and units that currently have bounced cheques."""
    bounced_collections = Collection.objects.filter(
        contract__unit__property_asset__owner_id=landlord_id,
        status='Bounced'
    ).select_related('contract__tenant', 'contract__unit', 'contract__unit__property_asset')

    if not bounced_collections.exists():
        return "Good news! There are currently no bounced cheques in the portfolio."

    bounced_data = {}
    for col in bounced_collections:
        tenant_name = col.contract.tenant.full_name
        unit_no = col.contract.unit.unit_number
        bldg = col.contract.unit.property_asset.name

        key = f"**Unit {unit_no}** in {bldg} ({tenant_name})"
        if key not in bounced_data:
            bounced_data[key] = 0
        bounced_data[key] += 1

    results = ["**Units with Bounced Cheques:**\n"]
    for key, count in bounced_data.items():
        results.append(f"- {key}: {count} bounced cheque(s)")

    return "\n".join(results)


@tool
def get_expiring_contracts(landlord_id: int, days_out: int = 90) -> str:
    """Returns a list of contracts expiring within the next specified number of days (default 90)."""
    target_date = timezone.now().date() + timedelta(days=days_out)

    expiring = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        status='Active',
        end_date__lte=target_date
    ).select_related('tenant', 'unit', 'unit__property_asset').order_by('end_date')

    if not expiring.exists():
        return f"There are no contracts expiring in the next {days_out} days."

    results = [f"**Contracts Expiring in the Next {days_out} Days:**\n"]
    for c in expiring:
        days_left = (c.end_date - timezone.now().date()).days
        results.append(
            f"- **{c.unit.unit_number}** ({c.unit.property_asset.name}) | Tenant: {c.tenant.full_name} | Expires: {c.end_date.strftime('%d %b %Y')} ({days_left} days left)")

    return "\n".join(results)


# ========================================================================
# 4. EXPENSES, CASHFLOW, AND NAVIGATION
# ========================================================================
@tool
def get_maintenance_expenses(landlord_id: int, property_name: str = None, unit_number: str = None, supplier: str = None,
                             date_from: str = None, date_to: str = None) -> str:
    """Queries owner/maintenance expenses. Can be filtered by building name, unit number, supplier, or date range."""
    expenses = OwnerExpense.objects.filter(landlord_id=landlord_id)

    # 🚀 THE FIX: Mirror the UI's strict condition to hide personal properties (Farm, Villa, etc.)
    allowed_towers = list(Property.objects.filter(owner_id=landlord_id).values_list('name', flat=True))
    allowed_towers.append("General Expenses")
    expenses = expenses.filter(tower_name__in=allowed_towers)

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            expenses = expenses.filter(tower_name__icontains=matched_prop.name)
        else:
            expenses = expenses.filter(tower_name__icontains=property_name)

    if unit_number: expenses = expenses.filter(unit_name__icontains=unit_number)
    if supplier: expenses = expenses.filter(supplier__icontains=supplier)
    if date_from: expenses = expenses.filter(date__gte=date_from)
    if date_to: expenses = expenses.filter(date__lte=date_to)

    total_spent = expenses.aggregate(total=Sum('amount'))['total'] or 0.0
    if not expenses.exists(): return "No maintenance or owner expenses found matching the criteria."

    lines = [f"**Total Expenses Found:** AED {float(total_spent):,.2f}\n\n**Recent Records:**"]
    for item in expenses.order_by('-date')[:5]:
        lines.append(
            f"- {item.date}: AED {item.amount:,.2f} | {item.tower_name} ({item.unit_name}) | Supplier: {item.supplier} | Ref: {item.voucher_no}"
        )

    if expenses.count() > 5:
        lines.append(f"\n*...and {expenses.count() - 5} more transactions.*")

    return "\n".join(lines)


@tool
def get_cashflow_summary(landlord_id: int, date_from: str = None, date_to: str = None) -> str:
    """Summarizes bank deposits, cleared vouchers, bounced cheques, and replaced payments from the Cashflow ledger."""
    landlord = Landlord.objects.filter(id=landlord_id).first()
    if not landlord: return "Landlord not found."
    vouchers = BankDeposit.objects.filter(Q(linked_landlord=landlord) | Q(linked_property__owner=landlord))
    if date_from: vouchers = vouchers.filter(date__gte=date_from)
    if date_to: vouchers = vouchers.filter(date__lte=date_to)

    total_deposited = vouchers.filter(doc_abbr__in=['BRV', 'CRT', 'CRVM']).aggregate(total=Sum('amount'))[
                          'total'] or 0.0
    total_bounced = vouchers.filter(doc_abbr='CBR').aggregate(total=Sum('amount'))['total'] or 0.0
    total_replaced = vouchers.filter(pact_status_id='429').aggregate(total=Sum('amount'))['total'] or 0.0

    return (
        f"**Cashflow Summary{' from ' + date_from if date_from else ''}{' to ' + date_to if date_to else ''}:**\n"
        f"- Total Deposited / Cleared: AED {float(total_deposited):,.2f}\n"
        f"- Total Bounced Vouchers: AED {float(total_bounced):,.2f}\n"
        f"- Total Replaced PDCs: AED {float(total_replaced):,.2f}\n"
        f"- Total Vouchers Processed: {vouchers.count()}"
    )


# ========================================================================
# 5. APPROVALS, DOCUMENTS, AND ADMIN
# ========================================================================

@tool
def get_pending_approvals(landlord_id: int) -> str:
    """Checks for any pending/unapproved voucher documents waiting for the landlord's sign-off."""
    approvals = ApprovalDocument.objects.filter(
        Q(landlord_id=landlord_id) | Q(landlord__isnull=True),
        status='UnApproved'
    ).order_by('-doc_date')

    if not approvals.exists():
        return "You have 0 pending approvals. All caught up!"

    results = [f"**You have {approvals.count()} pending approvals:**\n"]
    for a in approvals[:10]:
        results.append(f"- **{a.doc_no}** ({a.doc_date}): AED {float(a.amount):,.2f} | Type: {a.doc_type}")

    if approvals.count() > 10:
        results.append(f"\n*...and {approvals.count() - 10} more pending documents.*")

    return "\n".join(results)


@tool
def search_documents(landlord_id: int, search_query: str) -> str:
    """Searches for uploaded files and documents in the File Drive and Property Vault."""
    files = DocumentFile.objects.filter(owner_id=landlord_id, title__icontains=search_query)
    prop_docs = PropertyDocument.objects.filter(property_asset__owner_id=landlord_id, title__icontains=search_query)

    if not files.exists() and not prop_docs.exists():
        return f"No documents or files found matching '{search_query}'."

    results = [f"**Documents matching '{search_query}':**\n"]
    for f in files:
        folder_name = f.folder.name if f.folder else "Root Drive"
        results.append(f"- [File Drive] **{f.title}** (Folder: {folder_name})")

    for p in prop_docs:
        results.append(f"- [Property Vault] **{p.title}** (Building: {p.property_asset.name})")

    return "\n".join(results)


@tool
def get_last_sync_status() -> str:
    """Checks when the system last synced data from the PACT ERP."""
    latest_sync = SyncLog.objects.order_by('-timestamp').first()
    if not latest_sync:
        return "No synchronization logs found. The system may have never synced."

    time_str = latest_sync.timestamp.astimezone().strftime("%d %b %Y at %I:%M %p")
    return f"The portal database was last synced with PACT on **{time_str}** (Status: {latest_sync.status})."


@tool
def get_portal_navigation_guide(query_topic: str) -> str:
    """Provides navigation instructions and explanations of portal sections."""
    topic = query_topic.lower()
    navigation_map = {
        "vault": "Navigate to the **Vault** (`/owner/<id>/vault/`) to view all PDC transactions, cheque clearing history, and payees.",
        "cashflow": "Go to **Cashflow** (`/owner/<id>/cashflow/`) to monitor bank deposits, cash reconciliation (BRVs), and bounced cheques.",
        "reports": "Visit **Reports** (`/owner/<id>/reports/`) to view monthly collection trends, billed vs collected charts, and yearly rent growth analytics.",
        "health": "Check **Tenant Health** (`/owner/<id>/health/`) to view risk grades, payment lag flags, and contracts expiring within 90 days.",
        "occupancy": "Open **Occupancy Status** (`/owner/<id>/occupancy/`) for a breakdown of occupied vs vacant units, flats, shops, and parkings.",
        "expenses": "Access **Expenses** (`/owner/<id>/expenses/`) to review maintenance costs by tower, unit, or supplier.",
        "approvals": "Go to **Approvals** (`/owner/<id>/approvals/`) to approve or reject pending vouchers.",
        "drive": "Use **Drive** (`/owner/<id>/drive/`) to manage folders and upload lease/unit documents."
    }

    for key, text in navigation_map.items():
        if key in topic: return text

    return (
        "Here are the primary sections of the portal:\n"
        "- **Assets & Occupancy**: Overview of properties, units, and vacancy rates.\n"
        "- **Tenant Health**: Risk evaluation, payment delays, and renewal tracking.\n"
        "- **Vault & Cashflow**: Cheque tracking, collections, and bank deposits.\n"
        "- **Reports**: Financial analytics, rent growth, and collection trends.\n"
        "- **Expenses**: Maintenance and general ledger costs.\n"
        "- **Drive & Approvals**: Document repository and voucher sign-offs."
    )