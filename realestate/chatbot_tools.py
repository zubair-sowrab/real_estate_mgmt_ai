import difflib
from datetime import datetime, timedelta
import re
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncYear
from django.utils import timezone
from langchain.tools import tool

from .models import (
    ApprovalDocument, BankDeposit, Collection, Contract,
    ContractLedger, Landlord, OwnerExpense, Property,
    Tenant, Unit, SyncLog, DocumentFile, PropertyDocument
)
from .views import get_property_expanded_stats


# ========================================================================
# 0. UNIVERSAL HELPERS
# ========================================================================

def get_matched_property(landlord_id: int, search_query: str):
    if not search_query: return None
    properties = Property.objects.filter(owner_id=landlord_id)
    query_clean = search_query.lower().replace('building', '').replace('bldg', '').replace('tower', '').strip()

    for p in properties:
        if query_clean in p.name.lower(): return p

    query_words = query_clean.replace('al ', '').split()
    for p in properties:
        db_words = p.name.lower().replace('al ', '').replace(' bldg', '').split()
        for qw in query_words:
            if difflib.get_close_matches(qw, db_words, n=1, cutoff=0.6): return p
    return None


def fuzzy_filter_contracts(contracts_qs, search_query):
    """Bulletproof fuzzy matcher for tenants and units."""
    if not search_query: return contracts_qs.none()

    query_clean = search_query.lower().replace('flat', '').replace('shop', '').replace('unit', '').strip()

    # 1. Try Exact match across name, phone, or unit number
    exact = contracts_qs.filter(
        Q(tenant__full_name__icontains=search_query) |
        Q(tenant__phone__icontains=search_query) |
        Q(unit__unit_number__icontains=query_clean)
    )
    if exact.exists(): return exact

    # 2. Smart Word Matcher (Catches "Bakery" -> "Bakeries L.L.C")
    # Strip common fluff words and find the core search terms
    words = [w for w in search_query.lower().replace('al ', '').split() if len(w) >= 3]

    if words:
        q_obj = Q()
        for w in words:
            # Strip endings like 'y' or 'ies' to match the root word
            clean_w = w.rstrip('y').rstrip('ies') if len(w) > 4 else w
            q_obj |= Q(tenant__full_name__icontains=clean_w)
            q_obj |= Q(unit__unit_number__icontains=clean_w)

        fuzzy = contracts_qs.filter(q_obj)
        if fuzzy.exists(): return fuzzy

    return contracts_qs.none()
# ========================================================================
# 1. CORE PROPERTY & TENANT TOOLS (CURRENT ACTIVE CONTRACTS ONLY)
# ========================================================================
@tool
def get_current_tenant_info(landlord_id: int, search_query: str, property_name: str = None) -> str:
    """Retrieves phone number, email, and details of the CURRENT ACTIVE tenant in a specific unit or by name."""
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        status__in=['Active', 'Expired (Holdover)']
    ).select_related('tenant', 'unit', 'unit__property_asset')

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contracts = contracts.filter(unit__property_asset_id=matched_prop.id)

    # 🚀 THE FIX: Apply the smart fuzzy matcher
    contracts = fuzzy_filter_contracts(contracts, search_query)

    if not contracts.exists():
        return f"Could not find any CURRENT active tenant matching '{search_query}'."

    results = []
    for c in contracts:
        results.append(
            f"**Tenant:** {c.tenant.full_name}\n"
            f"- **Unit:** {c.unit.unit_number} ({c.unit.property_asset.name})\n"
            f"- **Phone:** {c.tenant.phone}\n"
            f"- **Email:** {c.tenant.email or 'N/A'}\n"
            f"- **Lease:** {c.start_date.strftime('%b %Y')} to {c.end_date.strftime('%b %Y')} ({c.status})"
        )
    return "\n\n".join(results)


@tool
def get_contract_balance(landlord_id: int, unit_number: str, property_name: str = None) -> str:
    """Retrieves the financial status of a CURRENT contract by calculating actual ledger debits and credits."""
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        status__in=['Active', 'Expired (Holdover)']
    ).select_related('tenant', 'unit')

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contracts = contracts.filter(unit__property_asset_id=matched_prop.id)

    # 🚀 THE FIX: Apply the smart fuzzy matcher here too
    contracts = fuzzy_filter_contracts(contracts, unit_number)

    if not contracts.exists():
        return f"No active contract found for unit/tenant '{unit_number}'."

    c = contracts.order_by('-start_date').first()

    ledgers = ContractLedger.objects.filter(contract=c)
    total_debit = sum(float(l.amount) for l in ledgers if l.is_debit)
    total_credit = sum(float(l.amount) for l in ledgers if not l.is_debit)
    total_pdr = float(sum(col.amount for col in c.collections.filter(status='Pending PDC')))

    balance = total_debit - total_credit

    return (
        f"**Tenant:** {c.tenant.full_name} | **Unit:** {c.unit.unit_number}\n"
        f"- **Total Amount Debited (Total Rent):** AED {total_debit:,.2f}\n"
        f"- **Total Amount Credited (Amount Paid):** AED {total_credit:,.2f}\n"
        f"- **Total Pending PDCs (PDR):** AED {total_pdr:,.2f}\n"
        f"- **Remaining Balance:** AED {balance:,.2f}\n"
        f"- **Contract Status:** {c.status}"
    )


@tool
def get_tenant_performance(landlord_id: int, search_query: str) -> str:
    """Evaluates tenant risk score, payment timeliness, bounced cheques, contract expiry, and maintenance cost ratio."""

    if not search_query or len(search_query.strip()) < 2:
        return "Please provide a specific tenant name or unit number."

    today = timezone.now().date()

    # 🚀 THE FIX: Use the smart fuzzy matcher instead of strict icontains
    contracts = Contract.objects.filter(unit__property_asset__owner_id=landlord_id).select_related('tenant', 'unit',
                                                                                                   'unit__property_asset')
    contracts = fuzzy_filter_contracts(contracts, search_query)

    if not contracts.exists():
        return f"No tenant or contract records found matching '{search_query}'."

    results = []
    for c in contracts:
        # 🚀 Pull financials perfectly
        ledgers = ContractLedger.objects.filter(contract=c)
        total_debit = sum(float(l.amount) for l in ledgers if l.is_debit)
        total_credit = sum(float(l.amount) for l in ledgers if not l.is_debit)
        total_pdr = float(sum(col.amount for col in c.collections.filter(status='Pending PDC')))

        # Fallback to contract base rent ONLY if ledger is completely empty
        total_rent = total_debit if total_debit > 0 else (float(c.comprehensive_rent_amount) or float(c.unit.base_rent))

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

        cleared_pct = min(100, int((total_credit / total_rent) * 100)) if total_rent > 0 else 0
        exp_agg = OwnerExpense.objects.filter(
            landlord_id=landlord_id,
            unit_name=c.unit.unit_number,
            tower_name=c.unit.property_asset.name,
            date__gte=c.start_date if c.start_date else datetime(2000, 1, 1).date()
        ).aggregate(total=Sum('amount'))
        expenses = float(exp_agg['total'] or 0)

        issues = []
        if bounced_count > 0: issues.append(f"{bounced_count} Bounced Cheque(s)")
        if cleared_pct < (time_pct - 15) and (total_credit + total_pdr) < total_rent * 0.95: issues.append(
            f"Payments lag behind time elapsed")
        if days_expired > 0: issues.append(f"Expired {days_expired} days ago")
        if expenses > (total_rent * 0.15) and expenses > 0: issues.append(f"High maintenance")

        grade = "At Risk" if (bounced_count > 0 or days_expired > 0 or (
                cleared_pct < (time_pct - 25) and (total_credit + total_pdr) < total_rent * 0.95)) else (
            "Excellent" if not issues and (
                        cleared_pct >= time_pct or (total_credit + total_pdr) >= total_rent * 0.95) else (
                "Warning" if issues else "Good"))

        # 🚀 THE FIX: Removed unnecessary padding to keep the response super compact
        results.append(
            f"**{c.tenant.full_name}** ({c.unit.unit_number})\n"
            f"- **Grade:** {grade}\n"
            f"- **Total Rent (Debit):** AED {total_rent:,.2f}\n"
            f"- **Paid (Credit):** AED {total_credit:,.2f}\n"
            f"- **Pending PDCs:** AED {total_pdr:,.2f}\n"
            f"- **Status:** {c.status}"
        )
    return "\n\n".join(results)


@tool
def get_unit_rent_history(landlord_id: int, unit_number: str, property_name: str = None) -> str:
    """Retrieves the complete historical list of past contracts and rent amounts for a specific unit."""
    contracts = Contract.objects.filter(unit__property_asset__owner_id=landlord_id)
    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop: contracts = contracts.filter(unit__property_asset_id=matched_prop.id)

    clean_unit = unit_number.lower().replace('flat', '').replace('shop', '').replace(' ', '')
    contracts = contracts.filter(unit__unit_number__icontains=clean_unit).select_related('tenant', 'unit').order_by(
        '-start_date')

    if not contracts.exists(): return f"No contract history found for unit '{unit_number}'."

    unit_ref = contracts.first().unit
    results = [f"**Rent History for Unit {unit_ref.unit_number} ({unit_ref.property_asset.name}):**\n"]
    for c in contracts:
        rent = float(c.comprehensive_rent_amount) if c.comprehensive_rent_amount else 0.0
        results.append(
            f"- **{c.start_date.strftime('%Y')} - {c.end_date.strftime('%Y')}**: AED {rent:,.2f} | Tenant: {c.tenant.full_name} ({c.status})")
    return "\n".join(results)


# ========================================================================
# 2. PORTFOLIO & TENANT HEALTH RANKINGS (THE "BEST/WORST" ENGINE)
# ========================================================================

@tool
def get_portfolio_tenant_health_ranking(landlord_id: int, rank_type: str = "worst", limit: int = 5) -> str:
    """
    Ranks ALL current tenants across the entire portfolio to find the absolute 'best' or 'worst'.
    Use this when the user asks 'who is my worst tenant', 'best tenants', 'who owes the most', etc.
    Rank type must be 'best' or 'worst'.
    """
    today = timezone.now().date()
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id,
        status__in=['Active', 'Expired (Holdover)']
    ).select_related('tenant', 'unit', 'unit__property_asset')

    tenant_scores = []

    for c in contracts:
        total_rent = float(c.comprehensive_rent_amount) or float(c.unit.base_rent)
        if total_rent == 0: continue

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

        cleared_pct = min(100, int((cleared / total_rent) * 100))
        expenses = float(OwnerExpense.objects.filter(
            landlord_id=landlord_id, unit_name=c.unit.unit_number, tower_name=c.unit.property_asset.name
        ).aggregate(total=Sum('amount'))['total'] or 0)

        # 🚀 THE HEALTH SCORING ALGORITHM
        # Lower score = Worse Tenant. Higher score = Better Tenant.
        score = 100
        score -= (bounced_count * 30)  # Heavy penalty for bounced cheques
        score -= (days_expired * 2)  # Penalty for expired contracts

        # Payment Lag Penalty
        lag = time_pct - cleared_pct
        if lag > 15 and (cleared + pdc) < total_rent * 0.95:
            score -= (lag * 1.5)

        # Maintenance Cost Penalty
        exp_ratio = expenses / total_rent
        if exp_ratio > 0.10:
            score -= (exp_ratio * 100)

        issues = []
        if bounced_count > 0: issues.append(f"{bounced_count} Bounced Cheque(s)")
        if lag > 15: issues.append(f"Payments lag {lag}% behind time elapsed")
        if expenses > (total_rent * 0.15): issues.append(f"High maintenance (AED {expenses:,.0f})")

        tenant_scores.append({
            'name': c.tenant.full_name,
            'unit': f"{c.unit.unit_number} ({c.unit.property_asset.name})",
            'score': score,
            'issues': ", ".join(issues) if issues else "Perfect profile",
            'rent': total_rent,
            'cleared_pct': cleared_pct
        })

    if rank_type.lower() == "worst":
        sorted_tenants = sorted(tenant_scores, key=lambda x: x['score'])
        title = f"🚨 **Top {limit} Most At-Risk/Worst Tenants in Portfolio:**"
    else:
        sorted_tenants = sorted(tenant_scores, key=lambda x: x['score'], reverse=True)
        title = f"🏆 **Top {limit} Best Performing Tenants in Portfolio:**"

    results = [title + "\n"]
    for idx, t in enumerate(sorted_tenants[:limit]):
        results.append(
            f"{idx + 1}. **{t['name']}** [{t['unit']}]\n   - Rent: AED {t['rent']:,.2f} ({t['cleared_pct']}% Cleared)\n   - Justification: {t['issues']}")

    return "\n\n".join(results)


@tool
def compare_portfolio_properties(landlord_id: int, property_names: str = None) -> str:
    """
    Compares all buildings owned by the landlord based on occupancy, rent, and expenses.
    If property_names is empty, it compares ALL properties automatically.
    """
    properties = Property.objects.filter(owner_id=landlord_id)

    if property_names:
        names = [n.strip() for n in property_names.split(',')]
        matched_ids = [get_matched_property(landlord_id, n).id for n in names if get_matched_property(landlord_id, n)]
        if matched_ids: properties = properties.filter(id__in=matched_ids)

    results = ["**Portfolio Property Comparison:**\n"]

    for prop in properties:
        _, total_count, occupied_count, occ_rate = get_property_expanded_stats(prop)
        billed = float(ContractLedger.objects.filter(contract__unit__property_asset=prop, is_debit=True).aggregate(
            total=Sum('amount'))['total'] or 0.0)
        expenses = float(
            OwnerExpense.objects.filter(landlord_id=landlord_id, tower_name__icontains=prop.name).aggregate(
                total=Sum('amount'))['total'] or 0.0)

        results.append(
            f"**{prop.name}**\n"
            f"- **Occupancy:** {occ_rate}% ({occupied_count} out of {total_count} units occupied)\n"
            f"- **Billed Rent:** AED {billed:,.2f}\n"
            f"- **Maintenance Expenses:** AED {expenses:,.2f}\n"
            f"- **Profit Margin:** AED {(billed - expenses):,.2f}\n"
        )
    return "\n".join(results)


# ========================================================================
# 3. CASHFLOW & EXACT DATE FILTERS
# ========================================================================

@tool
def get_cashflow_summary(landlord_id: int, exact_date: str = None, date_from: str = None, date_to: str = None) -> str:
    """
    Retrieves exactly how much money was deposited (cleared), bounced, or replaced.
    Supports 'exact_date' (YYYY-MM-DD) OR 'date_from' and 'date_to'.
    """
    landlord = Landlord.objects.filter(id=landlord_id).first()
    vouchers = BankDeposit.objects.filter(Q(linked_landlord=landlord) | Q(linked_property__owner=landlord))

    # 🚀 EXACT DATE PRECISION
    if exact_date:
        vouchers = vouchers.filter(date=exact_date)
        time_context = f"on {exact_date}"
    else:
        if date_from: vouchers = vouchers.filter(date__gte=date_from)
        if date_to: vouchers = vouchers.filter(date__lte=date_to)
        time_context = f"from {date_from or 'Start'} to {date_to or 'Today'}"

    deposited = vouchers.filter(doc_abbr__in=['BRV', 'CRT', 'CRVM']).aggregate(total=Sum('amount'))['total'] or 0.0
    bounced = vouchers.filter(doc_abbr='CBR').aggregate(total=Sum('amount'))['total'] or 0.0

    return (
        f"**Cashflow Summary {time_context}:**\n"
        f"- **Successfully Deposited/Cleared:** AED {float(deposited):,.2f}\n"
        f"- **Bounced Vouchers:** AED {float(bounced):,.2f}\n"
        f"- **Total Transactions Processed:** {vouchers.count()}"
    )


# ========================================================================
# 4. NAVIGATION & OTHERS
# ========================================================================

@tool
def get_portal_navigation_guide() -> str:
    """Returns the EXACT mapping of the 7 available portal pages and what they are used for."""
    return (
        "Here are the 7 active pages in your EDRAK Portal and what you can do on them:\n"
        "1. **Dashboard:** View your overall cashflow and bank deposits (BRV, CRT, etc.).\n"
        "2. **Tenant Directory:** Search, filter, and view tenant contact details (phone, email).\n"
        "3. **File Drive:** Upload and manage property documents, tenant info, and general files.\n"
        "4. **Reports:** View analytics on buildings, rent growth, collections, and units.\n"
        "5. **Occupancy & Rent:** See building occupancy percentages, vacant/occupied unit counts, unit types (flats/shops), and specific lease balances from the ledger.\n"
        "6. **Expenses:** Review maintenance costs and general expenses filtered by building, unit, or supplier.\n"
        "7. **Tenant Health:** A highly visual dashboard showing contract time elapsed vs. rent cleared, ledger details, and risk ratings (Excellent, Good, Warning, At Risk) with exact justifications."
    )