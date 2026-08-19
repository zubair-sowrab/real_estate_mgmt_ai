import difflib
from datetime import datetime, timedelta
from typing import Optional
import re
from django.db.models import Avg, Count, Q, Sum
from django.db.models import Q
from django.db.models.functions import TruncYear
from django.utils import timezone
import itertools
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
def get_current_tenant_info(landlord_id: int, search_query: str, property_name: str = "") -> str:
    """Retrieves phone number, email, and details of the LATEST tenant in a specific unit or by name."""

    # 🚀 THE FIX: Remove the strict status filter so we can scan all contracts,
    # but we will sort them to guarantee we only return the absolute newest one.
    contracts = Contract.objects.filter(
        unit__property_asset__owner_id=landlord_id
    ).select_related('tenant', 'unit', 'unit__property_asset')

    if property_name:
        matched_prop = get_matched_property(landlord_id, property_name)
        if matched_prop:
            contracts = contracts.filter(unit__property_asset_id=matched_prop.id)

    # Apply the smart fuzzy matcher
    contracts = fuzzy_filter_contracts(contracts, search_query)

    if not contracts.exists():
        return f"Could not find any tenant matching '{search_query}'."

    # 🚀 THE FIX: Sort by start_date descending so the newest contract is ALWAYS first
    contracts = contracts.order_by('-start_date')

    seen_units = set()
    results = []

    # Only keep the absolute newest contract per physical unit
    for c in contracts:
        if c.unit.id in seen_units:
            continue
        seen_units.add(c.unit.id)

        start_str = c.start_date.strftime('%d %b %Y') if c.start_date else 'N/A'
        end_str = c.end_date.strftime('%d %b %Y') if c.end_date else 'N/A'

        # 🚀 THE FIX: Cross-reference physical Unit status and Contract status (Just like views.py & pact_sync.py)
        is_vacant = (c.unit.status == 'Vacant') or (c.status in ['Closed', 'Terminated'])

        if is_vacant:
            unit_label = f"{c.unit.unit_number} ({c.unit.property_asset.name}) 🚨 [CURRENTLY VACANT]"
            real_status = f"{c.status} (Moved Out)"
        else:
            unit_label = f"{c.unit.unit_number} ({c.unit.property_asset.name})"
            real_status = c.status
            if real_status == 'Active' and c.end_date and c.end_date < timezone.now().date():
                real_status = 'Expired (Holdover)'

        results.append(
            f"**Unit:** {unit_label}\n"
            f"- **Previous/Latest Tenant:** {c.tenant.full_name}\n"
            f"- **Phone:** {c.tenant.phone}\n"
            f"- **Email:** {c.tenant.email or 'N/A'}\n"
            f"- **Lease:** {start_str} to {end_str} ({real_status})"
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


@tool
def get_portfolio_tenant_health_ranking(landlord_id: int, rank_type: str = "worst", limit: int = 5) -> str:
    """
    Ranks ALL true current tenants across the entire portfolio to find the absolute 'best' or 'worst'.
    """
    today = timezone.now().date()
    properties = Property.objects.filter(owner_id=landlord_id)

    true_active_contracts = []

    for p in properties:
        exp_units, _, _, _ = get_property_expanded_stats(p)
        sno_groups = {}
        ghost_unit_ids = set()

        for eu in exp_units:
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                sno = c.pact_costcenter_id
                if sno and sno not in ['0', '1', 'None', '']:
                    if sno not in sno_groups: sno_groups[sno] = []
                    sno_groups[sno].append(eu)

        for sno, eu_list in sno_groups.items():
            if len(eu_list) > 1:
                master_eu = max(eu_list, key=lambda x: len(x['original_unit'].unit_number))
                for eu in eu_list:
                    if eu != master_eu: ghost_unit_ids.add(eu['unit_number'])

        for eu in exp_units:
            if eu['unit_number'] in ghost_unit_ids: continue
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                true_active_contracts.append(c)

    tenant_scores = []

    for c in true_active_contracts:
        ledgers = ContractLedger.objects.filter(contract=c)
        total_debit = sum(float(l.amount) for l in ledgers if l.is_debit)
        total_credit = sum(float(l.amount) for l in ledgers if not l.is_debit)
        total_pdr = float(sum(col.amount for col in c.collections.filter(status='Pending PDC')))

        total_rent = total_debit if total_debit > 0 else (float(c.comprehensive_rent_amount) or float(c.unit.base_rent))
        if total_rent == 0: continue

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

        cleared_pct = min(100, int((total_credit / total_rent) * 100))
        expenses = float(OwnerExpense.objects.filter(
            landlord_id=landlord_id, unit_name=c.unit.unit_number, tower_name=c.unit.property_asset.name
        ).aggregate(total=Sum('amount'))['total'] or 0)

        # 🚀 THE HEALTH SCORING ALGORITHM
        score = 100
        score -= (bounced_count * 30)
        score -= (days_expired * 2)

        lag = time_pct - cleared_pct
        if lag > 15 and (total_credit + total_pdr) < total_rent * 0.95:
            score -= (lag * 1.5)

        exp_ratio = expenses / total_rent
        if exp_ratio > 0.10:
            score -= (exp_ratio * 100)

        # 🚀 THE FIX: Sync issues exactly to the penalties so it never lies
        issues = []
        if bounced_count > 0: issues.append(f"{bounced_count} Bounced Cheque(s)")
        if days_expired > 0: issues.append(f"Expired {days_expired} days ago")
        if lag > 15 and (total_credit + total_pdr) < total_rent * 0.95: issues.append(
            f"Payments lag {lag}% behind time elapsed")
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
        # 🚀 THE FIX: Only include tenants who actually lost points (score < 100)
        bad_tenants = [t for t in tenant_scores if t['score'] < 100]
        sorted_tenants = sorted(bad_tenants, key=lambda x: x['score'])

        if not sorted_tenants:
            return "🎉 Excellent news! You currently have no 'At-Risk' or poorly performing tenants in your portfolio."

        actual_limit = min(limit, len(sorted_tenants))
        title = f"🚨 **Top {actual_limit} Most At-Risk/Worst Tenants in Portfolio:**"
    else:
        # Best tenants list
        good_tenants = [t for t in tenant_scores if t['score'] >= 100]
        sorted_tenants = sorted(good_tenants, key=lambda x: x['score'], reverse=True)

        if not sorted_tenants:
            sorted_tenants = sorted(tenant_scores, key=lambda x: x['score'], reverse=True)

        actual_limit = min(limit, len(sorted_tenants))
        title = f"🏆 **Top {actual_limit} Best Performing Tenants in Portfolio:**"

    results = [title + "\n"]
    for idx, t in enumerate(sorted_tenants[:actual_limit]):
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


@tool
def get_cashflow_summary(landlord_id: int, exact_date: Optional[str] = None, date_from: Optional[str] = None,
                         date_to: Optional[str] = None) -> str:
    """
    Retrieves exactly how much money was deposited (cleared), bounced, or replaced, ALONG WITH the detailed line-by-line voucher breakdown (including nested CROs).
    Supports 'exact_date' (YYYY-MM-DD) OR 'date_from' and 'date_to'.
    """
    landlord = Landlord.objects.filter(id=landlord_id).first()
    if not landlord: return "Landlord not found."

    # 🚀 1. THE BULLETPROOF SHIELD
    pact_ll_id = str(landlord.pact_landlord_id).strip()
    prefix_map = {'14': ['05'], '15': ['07'], '16': ['08'], '17': ['02'], '18': ['09'], '19': ['06']}

    primary_prefixes = prefix_map.get(pact_ll_id, [])
    allowed_prefixes = list(primary_prefixes)
    if '02' not in allowed_prefixes: allowed_prefixes.append('02')
    if '01' not in allowed_prefixes: allowed_prefixes.append('01')

    if primary_prefixes:
        final_query = Q(doc_prefix__in=primary_prefixes)
    else:
        final_query = Q(pk__in=[])

    building_query = Q(linked_property__owner=landlord)
    for p in Property.objects.filter(owner=landlord):
        core_name = p.name.replace('Bldg', '').replace('Building', '').replace('Tower', '').strip()
        if core_name and len(core_name) > 3:
            building_query |= Q(tower__icontains=core_name)
            building_query |= Q(narration__icontains=core_name)

    final_query |= (building_query & Q(doc_prefix__in=allowed_prefixes))

    vouchers = BankDeposit.objects.filter(final_query).exclude(doc_prefix__in=['01', '03']).distinct()

    # 🚀 2. GRAB ALL HISTORY FOR PROPER CRO MATCHING BEFORE DATE FILTERING
    all_cros = list(vouchers.filter(doc_abbr__in=['CRV', 'CRO']).order_by('date', 'id'))
    all_deposits = list(vouchers.filter(doc_abbr__in=['BRV', 'CRT', 'CRVM', 'CBR']).order_by('date', 'id'))
    unmatched_cros = all_cros.copy()

    TOLERANCE = 10.0
    consolidated_deposits = []
    cash_brv_dict = {}

    for d in all_deposits:
        if d.doc_abbr == 'BRV':
            has_cheque = bool(d.cheque_no and str(d.cheque_no).strip().lower() not in ['none', ''])
            has_date = bool(d.cheque_date and str(d.cheque_date).strip().lower() not in ['none', ''])
            if has_cheque or has_date:
                consolidated_deposits.append(d)
            else:
                if d.date not in cash_brv_dict: cash_brv_dict[d.date] = []
                cash_brv_dict[d.date].append(d)
        else:
            consolidated_deposits.append(d)

    for dt, brvs in cash_brv_dict.items():
        if len(brvs) == 1:
            consolidated_deposits.append(brvs[0])
        else:
            primary = brvs[0]
            primary.amount = sum(float(b.amount) for b in brvs)
            if '/' in primary.voucher_no:
                prefix = primary.voucher_no.rsplit('/', 1)[0] + '/'
                suffixes = [b.voucher_no.rsplit('/', 1)[-1] for b in brvs if '/' in b.voucher_no]
                primary.voucher_no = f"{prefix}{' & '.join(suffixes)}"
            else:
                primary.voucher_no = " & ".join([b.voucher_no for b in brvs])
            consolidated_deposits.append(primary)

    consolidated_deposits.sort(key=lambda x: x.date)

    # 🚀 3. FIND THE MATCHING CROS
    for d in consolidated_deposits:
        d.linked_cros = []
        if d.doc_abbr != 'BRV': continue

        has_cheque = bool(d.cheque_no and str(d.cheque_no).strip().lower() not in ['none', ''])
        has_date = bool(d.cheque_date and str(d.cheque_date).strip().lower() not in ['none', ''])
        if has_cheque or has_date: continue

        target = float(d.amount)
        matched = False
        valid_cros = sorted([c for c in unmatched_cros if c.date <= d.date and (d.date - c.date).days <= 14],
                            key=lambda x: (x.date, x.id), reverse=True)

        for cro in valid_cros:
            if abs(float(cro.amount) - target) <= TOLERANCE:
                d.linked_cros = [cro]
                unmatched_cros.remove(cro)
                matched = True
                break
        if matched: continue

        for i in range(len(valid_cros)):
            current_sum = 0.0
            temp_match = []
            for j in range(i, len(valid_cros)):
                current_sum += float(valid_cros[j].amount)
                temp_match.append(valid_cros[j])
                if abs(current_sum - target) <= TOLERANCE:
                    d.linked_cros = sorted(temp_match, key=lambda x: x.date)
                    for m in temp_match: unmatched_cros.remove(m)
                    matched = True
                    break
                elif current_sum > target + TOLERANCE:
                    break
            if matched: break
        if matched: continue

        if valid_cros:
            recent_cros = valid_cros[:20]
            combo_found = False
            for r in range(2, min(7, len(recent_cros) + 1)):
                for combo in itertools.combinations(recent_cros, r):
                    if abs(sum(float(c.amount) for c in combo) - target) <= TOLERANCE:
                        d.linked_cros = sorted(list(combo), key=lambda x: x.date)
                        for m in combo: unmatched_cros.remove(m)
                        combo_found = True
                        break
                if combo_found: break

    consolidated_deposits.sort(key=lambda x: x.date, reverse=True)

    # 🚀 4. NOW FILTER BY DATE (Preserves the matching done above!)
    final_ui_list = []
    time_context = "for requested dates"

    if exact_date:
        time_context = f"on {exact_date}"
        for d in consolidated_deposits:
            if str(d.date) == exact_date: final_ui_list.append(d)
    else:
        time_context = f"from {date_from or 'Start'} to {date_to or 'Today'}"
        for d in consolidated_deposits:
            keep = True
            if date_from and str(d.date) < date_from: keep = False
            if date_to and str(d.date) > date_to: keep = False
            if keep: final_ui_list.append(d)

    # 🚀 5. BUILD THE PERFECT RESPONSE TEXT
    deposited_total = 0.0
    bounced_total = 0.0
    cleared_lines = []
    bounced_lines = []

    for v in final_ui_list:
        if v.doc_abbr == 'CBR':
            bounced_total += float(v.amount)
            label = v.tenant or v.tower or v.narration or "Bounced Reversal"
            chq = f" | CHQ: {v.cheque_no}" if v.cheque_no and v.cheque_no.lower() not in ['none', ''] else ""
            bounced_lines.append(f"- **{v.voucher_no}** | AED {float(v.amount):,.2f} | {label}{chq}")
        else:
            deposited_total += float(v.amount)
            chq = f" | CHQ: {v.cheque_no}" if v.cheque_no and v.cheque_no.lower() not in ['none', ''] else ""

            # 🚀 THE FIX: Completely block narrations/remarks for BRV
            if v.doc_abbr == 'BRV':
                label = v.tenant or v.tower or ""
            else:
                label = v.tenant or v.tower or v.narration or "Bank Deposit"

            label_text = f" | {label}" if label else ""

            cro_details = ""
            if hasattr(v, 'linked_cros') and v.linked_cros:
                cros = []
                for cro in v.linked_cros:
                    c_label = cro.tenant or cro.narration or ""
                    c_label_text = f" - {c_label}" if c_label else ""
                    cros.append(f"  * ↳ **{cro.voucher_no}** (AED {float(cro.amount):,.2f}){c_label_text}")
                cro_details = "\n" + "\n".join(cros)

            cleared_lines.append(f"- **{v.voucher_no}** | AED {float(v.amount):,.2f}{label_text}{chq}{cro_details}")

    results = [
        f"**Cashflow Summary {time_context}:**",
        f"- **Deposited/Cleared:** AED {deposited_total:,.2f}",
        f"- **Bounced Vouchers:** AED {bounced_total:,.2f}",
        f"- **Total Transactions Processed:** {len(final_ui_list)}\n",
    ]

    if cleared_lines:
        results.append("**✅ Cleared Deposits Breakdown:**")
        results.extend(cleared_lines)

    if bounced_lines:
        results.append("\n**🚨 Bounced Cheques Breakdown:**")
        results.extend(bounced_lines)

    return "\n".join(results)



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


@tool
def get_pending_pdcs(landlord_id: int, exact_date: str = "", date_from: str = "", date_to: str = "") -> str:
    """
    Retrieves a list of all Pending PDCs (post-dated cheques) expected for a specific date or date range.
    Use this when the user asks for 'pdcs for tomorrow', 'cheques coming up', etc.
    """
    collections = Collection.objects.filter(
        contract__unit__property_asset__owner_id=landlord_id,
        status='Pending PDC'
    ).select_related('contract__tenant', 'contract__unit', 'contract__unit__property_asset').order_by('due_date')

    if exact_date:
        collections = collections.filter(due_date=exact_date)
        time_context = f"for {exact_date}"
    else:
        if date_from: collections = collections.filter(due_date__gte=date_from)
        if date_to: collections = collections.filter(due_date__lte=date_to)
        time_context = f"from {date_from or 'Start'} to {date_to or 'Future'}"

    if not collections.exists():
        return f"No pending PDCs found {time_context}."

    total_amount = sum(float(c.amount) for c in collections)

    results = [
        f"**Expected PDCs {time_context}:**",
        f"- **Total Amount:** AED {total_amount:,.2f}",
        f"- **Total Cheques:** {collections.count()}\n",
        "**Cheque Breakdown:**"
    ]

    for c in collections:
        tenant = c.contract.tenant.full_name if c.contract.tenant else "Unknown Tenant"
        unit = c.contract.unit.unit_number if c.contract.unit else "Unknown Unit"
        bldg = c.contract.unit.property_asset.name if c.contract.unit and c.contract.unit.property_asset else ""
        chq_info = f"CHQ: {c.cheque_number}" if c.cheque_number else "No CHQ"

        date_str = c.due_date.strftime('%Y-%m-%d') if c.due_date else ""
        results.append(f"- **{date_str}** | AED {float(c.amount):,.2f} | {chq_info} | {tenant} ({unit}, {bldg})")

    return "\n".join(results)


@tool
def get_vacant_units(landlord_id: int, property_name: str = "") -> str:
    """
    Retrieves an accurate list of all currently VACANT units for a specific building.
    Use this when the user asks 'what is vacant in Muhammed Yousuf', 'show empty flats', etc.
    """
    properties = list(Property.objects.filter(owner_id=landlord_id))

    if property_name:
        search_lower = property_name.lower().replace('bldg', '').replace('building', '').strip()
        prop_map = {p.name.lower().replace('bldg', '').replace('building', '').strip(): p for p in properties}

        matched_prop = None

        # 1. Try direct substring match first
        for clean_name, p in prop_map.items():
            if search_lower in clean_name or clean_name in search_lower:
                matched_prop = p
                break

        # 2. Use Python's built-in fuzzy matcher to catch typos
        if not matched_prop:
            close_matches = difflib.get_close_matches(search_lower, prop_map.keys(), n=1, cutoff=0.5)
            if close_matches:
                matched_prop = prop_map[close_matches[0]]

        if matched_prop:
            properties = [matched_prop]
        else:
            available = ", ".join([p.name for p in properties])
            return f"Could not find a building matching '{property_name}'. Available properties are: {available}"

    results = []

    for p in properties:
        # 🚀 STEP 1: Unit Normalization & Latest Contract Check
        units = p.units.all()
        expanded_dict = {}

        for u in units:
            if '-' not in u.unit_number:
                continue

            clean_name = u.unit_number.split(':')[0].strip()
            if ',' in clean_name:
                split_units = sorted([x.strip() for x in clean_name.split(',')])
                clean_name = ','.join(split_units)

            if clean_name not in expanded_dict:
                expanded_dict[clean_name] = {
                    'unit_number': clean_name, 'is_occupied': False, 'tenant': None,
                    'original_unit': u, 'all_contracts': []
                }
            expanded_dict[clean_name]['all_contracts'].extend(list(u.contracts.all()))

        for sub, data in expanded_dict.items():
            if not data['all_contracts']:
                data['latest_contract'] = None
                continue

            newest_contract = max(data['all_contracts'], key=lambda c: (c.start_date, c.end_date))
            data['latest_contract'] = newest_contract

            if newest_contract.status not in ['Terminated', 'Closed']:
                data['is_occupied'] = True
                data['tenant'] = newest_contract.tenant

        exp_units = list(expanded_dict.values())

        # 🚀 STEP 2: The Shattered Lease Deduplicator (Cost Center ID Check)
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

        exp_units = [eu for eu in exp_units if eu['unit_number'] not in ghost_unit_ids]

        # 🚀 STEP 3: The Physical Space Deduplicator (Sub-Unit Check)
        occupied_base_units = set()
        for eu in exp_units:
            c = eu.get('latest_contract')
            if c and c.status in ['Active', 'Expired (Holdover)']:
                for su in [u.strip() for u in eu['unit_number'].split(',')]:
                    occupied_base_units.add(su)

        all_standalone_units = set(eu['unit_number'].strip() for eu in exp_units if ',' not in eu['unit_number'])

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

        exp_units = [eu for eu in exp_units if eu['unit_number'] not in physical_ghost_ids]

        # 🚀 STEP 4: Compile Final Vacancy List
        # 🚀 STEP 4: Compile Final Vacancy List (EXACT views.py matching)
        vacant_units = []
        for eu in exp_units:
            # 🚀 THE FIX: ONLY check is_occupied! Never trust the raw unit status over the lease logic!
            if not eu['is_occupied']:
                last_tenant = eu['tenant'].full_name if eu['tenant'] else "None"

                # Fetch the absolute most recent closed/terminated tenant name if no active one exists
                if not eu['tenant'] and eu.get('latest_contract') and eu['latest_contract'].tenant:
                    last_tenant = eu['latest_contract'].tenant.full_name

                vacant_units.append(f"- **{eu['unit_number']}** (Last Tenant: {last_tenant})")

        if vacant_units:
            vacant_units.sort()
            results.append(f"**{p.name} - Vacant Units ({len(vacant_units)}):**\n" + "\n".join(vacant_units))
        else:
            results.append(f"**{p.name}:** 100% Occupied (0 Vacant units)")

    return "\n\n".join(results)


@tool
def get_owner_expenses(landlord_id: int, exact_date: str = "", date_from: str = "", date_to: str = "",
                       property_name: str = "", search_query: str = "") -> str:
    """
    Retrieves general ledger expenses and maintenance costs.
    Use this ONLY when the user explicitly asks for "expenses", "costs", "maintenance", "petty cash", or money spent.
    """
    landlord = Landlord.objects.filter(id=landlord_id).first()
    if not landlord: return "Landlord not found."

    # 🚀 REPLICATE UI LOGIC: Restrict to official properties + General Expenses
    allowed_towers = list(Property.objects.filter(owner=landlord).values_list('name', flat=True))
    allowed_towers.append("General Expenses")

    expenses = OwnerExpense.objects.filter(landlord=landlord, tower_name__in=allowed_towers)

    # Date Filters
    if exact_date:
        expenses = expenses.filter(date=exact_date)
        time_context = f"on {exact_date}"
    else:
        if date_from: expenses = expenses.filter(date__gte=date_from)
        if date_to: expenses = expenses.filter(date__lte=date_to)
        time_context = f"from {date_from or 'Start'} to {date_to or 'Today'}"

    if property_name:
        expenses = expenses.filter(tower_name__icontains=property_name)

    if search_query:
        expenses = expenses.filter(
            Q(supplier__icontains=search_query) |
            Q(remarks__icontains=search_query) |
            Q(narration__icontains=search_query) |
            Q(voucher_no__icontains=search_query)
        )

    expenses = expenses.order_by('-date', '-id')

    if not expenses.exists():
        return f"No expenses found {time_context}."

    total_amount = sum(float(e.amount) for e in expenses)

    results = [
        f"**Expense Summary {time_context}:**",
        f"- **Total Expenses:** AED {total_amount:,.2f}",
        f"- **Total Transactions:** {expenses.count()}\n",
        "**Detailed Expense Breakdown:**"
    ]

    for e in expenses:
        date_str = e.date.strftime("%Y-%m-%d") if e.date else "N/A"
        tower = e.tower_name or "General Expenses"
        unit = e.unit_name or ""
        location = f"{tower} - {unit}".strip(" -")

        # Format the exact way the UI does (Doc + Line details)
        details = []
        if e.remarks: details.append(f"Doc: {e.remarks}")
        if e.narration: details.append(f"Line: {e.narration}")
        detail_str = " | ".join(details)

        results.append(
            f"- **{date_str}** | {e.voucher_no} | **AED {float(e.amount):,.2f}** | {e.supplier} | {location} | {detail_str}")

    return "\n".join(results)