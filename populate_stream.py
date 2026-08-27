import random
from datetime import timedelta
from realestate.models import Contract, ContractLedger

# 1. Clear out existing ledger entries to prevent duplicates
ContractLedger.objects.all().delete()
print("Cleared old contract ledgers.")

all_contracts = Contract.objects.all()
ledgers_to_create = []
banks_list = ["RUYA C.I.B L.L.C", "ADCB", "Emirates NBD", "First Abu Dhabi Bank"]

for con in all_contracts:
    rent_amount = float(con.comprehensive_rent_amount)
    if rent_amount <= 0:
        continue

    tracking_no = str(random.randint(1000, 9999))

    # A. Initial Debit Entry (Rent Charge / TCO)
    ledgers_to_create.append(ContractLedger(
        contract=con,
        doc_date=con.start_date,
        doc_no=f"TCO-{con.id:04d}",
        doc_abbr="TCO",
        debit_account="Rental Receivable",
        credit_account="Rental Income",
        amount=rent_amount,
        is_debit=True,
        particular=f"Annual Rent for Unit {con.unit.unit_number}",
        chq_no="",
        chq_date=None,
        bank="",
        tracking_no=tracking_no
    ))

    # B. Generate matching payment and collection entries (IRV / CRV / PDC)
    num_installments = random.choice([2, 4])
    installment_amt = rent_amount / num_installments

    for i in range(num_installments):
        pay_date = con.start_date + timedelta(days=i * (365 // num_installments))
        chq_no = f"{random.randint(100000, 999999)}"
        bank_name = random.choice(banks_list)

        # Credit entry (IRV/CRV Payment)
        ledgers_to_create.append(ContractLedger(
            contract=con,
            doc_date=pay_date,
            doc_no=f"IRV-{con.id:04d}-{i + 1}",
            doc_abbr="IRV",
            debit_account="Bank / Cash",
            credit_account="Rental Receivable",
            amount=installment_amt,
            is_debit=False,
            particular=f"PDC Receivable against Cheque no: {chq_no}",
            chq_no=chq_no,
            chq_date=pay_date,
            bank=bank_name,
            tracking_no=tracking_no
        ))

# Bulk create all ledger entries in safe batches of 50
ContractLedger.objects.bulk_create(ledgers_to_create, batch_size=50)
print(f"✅ Successfully generated {len(ledgers_to_create)} ContractLedger entries across all active contracts!")