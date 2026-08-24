from django.db import models
from django.utils import timezone

from django.utils import timezone
from django.contrib.auth.models import User


# ------------------------------------------------------------------------
# 1. LANDLORD / OWNER PROFILES
# Maps to backend logic for identifying who owns which REN_Property
# ------------------------------------------------------------------------
class Landlord(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='landlord_profile',
                                help_text="Link this landlord to a Django User account for login.")
    name = models.CharField(max_length=255)
    contact_email = models.EmailField(blank=True, null=True)
    contact_phone = models.CharField(max_length=50, blank=True, null=True)
    pact_landlord_id = models.CharField(max_length=100, unique=True, help_text="ID matching the PACT ERP system")

    def __str__(self):
        return self.name


# ------------------------------------------------------------------------
# 2. PROPERTIES (Buildings/Villas)
# Maps to PACT: REN_Property
# ------------------------------------------------------------------------
class Property(models.Model):
    pact_property_id = models.CharField(max_length=100, unique=True)
    owner = models.ForeignKey(Landlord, on_delete=models.CASCADE, related_name='properties')
    name = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True, null=True)
    total_units = models.IntegerField(default=0)
    # THE TRUE LEDGER LINK
    rental_receivable_account_id = models.CharField(max_length=100, blank=True, null=True)


    class Meta:
        verbose_name_plural = "Properties"

    def __str__(self):
        return f"{self.name} (Owner: {self.owner.name})"


# ------------------------------------------------------------------------
# 3. UNITS (Apartments/Shops)
# Maps to PACT: REN_Units
# ------------------------------------------------------------------------
class Unit(models.Model):
    STATUS_CHOICES = [
        ('Vacant', 'Vacant'),
        ('Occupied', 'Occupied'),
        ('Maintenance', 'Maintenance'),
    ]

    pact_unit_id = models.CharField(max_length=100, unique=True)
    property_asset = models.ForeignKey(Property, on_delete=models.CASCADE, related_name='units')
    # THE FIX: Increased from 50 to 255 to stop PACT strings from getting corrupted!
    unit_number = models.CharField(max_length=255)
    unit_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Vacant')
    base_rent = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    def __str__(self):
        return f"{self.property_asset.name} - Unit {self.unit_number}"


# ------------------------------------------------------------------------
# 4. TENANTS
# Maps to PACT: REN_Tenant
# ------------------------------------------------------------------------
class Tenant(models.Model):
    pact_tenant_id = models.CharField(max_length=100, unique=True)
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=50)
    emirates_id = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return self.full_name


# ------------------------------------------------------------------------
# 5. CONTRACTS / LEASES
# Maps to PACT: REN_Contract
# ------------------------------------------------------------------------
class Contract(models.Model):
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Expired (Holdover)', 'Expired (Holdover)'),
        ('Terminated', 'Terminated'),
        ('Closed', 'Closed'),
    ]

    pact_contract_id = models.CharField(max_length=100, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='contracts')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='contracts')

    start_date = models.DateField()
    end_date = models.DateField()

    total_amount = models.DecimalField(max_digits=12, decimal_places=2)  # Includes Tasdeeq, etc.

    # NEW: Separated Rent Fields
    rent_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    net_rent_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')

    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    rent_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # THE FIX: New fields to perfectly track base rent vs valid extras
    net_rent_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    comprehensive_rent_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    extra_charges_text = models.TextField(blank=True, null=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')

    pact_costcenter_id = models.CharField(max_length=100, blank=True, null=True)
    true_total_rent = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    true_total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    true_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    true_ledger_json = models.TextField(blank=True, null=True)

    def get_parsed_extras(self):
        """Safely unpacks the extra charges for the UI"""
        if not self.extra_charges_text: return []
        extras = []
        for ext in self.extra_charges_text.split(" | "):
            if ":" in ext:
                name, amt = ext.split(":")
                extras.append({'name': name.strip(), 'amount': float(amt.strip())})
        return extras

    @property
    def rent_collections(self):
        sorted_cols = sorted(self.collections.all(), key=lambda c: c.due_date)
        rent_items = []

        # VIP: Forcefully banish Tasdeeq (28), Sewerage (31), and Security Deposits!
        banned_ids = ['28', '31']
        banned_words = ['tasdeeq', 'sewerage', 'security', 'deposit']

        for c in sorted_cols:
            part_text = str(c.particular).strip().lower()

            # STRICT BLOCK
            if str(c.particular).strip() in banned_ids or any(k in part_text for k in banned_words):
                continue

            rent_items.append(c)

        return rent_items

    @property
    def total_paid(self):
        valid_statuses = ['Cleared', 'Posted', 'Pending PDC', 'Replaced/Partial', 'Replaced']
        return sum(c.amount for c in self.rent_collections if c.status in valid_statuses)

    @property
    def remaining_balance(self):
        # Uses the true combined rent target
        bal = self.comprehensive_rent_amount - self.total_paid
        return bal if bal > 0 else 0

    @property
    def cleared_paid(self):
        return sum(c.amount for c in self.rent_collections if c.status in ['Cleared', 'Posted'])

    @property
    def strict_balance(self):
        bal = self.comprehensive_rent_amount - self.cleared_paid
        return bal if bal > 0 else 0

    def __str__(self):
        return f"{self.tenant.full_name} - {self.unit.unit_number} ({self.status})"


# ------------------------------------------------------------------------
# 6. COLLECTION HISTORY / TRANSACTIONS
# Maps to PACT: REN_CollectionHistory / ACC_AccountsHistory
# ------------------------------------------------------------------------
class Collection(models.Model):
    STATUS_CHOICES = [
        ('Cleared', 'Cleared'),
        ('Pending PDC', 'Pending PDC'),
        ('Bounced', 'Bounced'),
    ]
    particular = models.CharField(max_length=255, default='Rent')
    pact_transaction_id = models.CharField(max_length=100, unique=True)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='collections')

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField()
    payment_type = models.CharField(max_length=50, help_text="e.g., Cheque, Bank Transfer, Cash")
    cheque_number = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending PDC')

    # NEW FIELD: Tracks if it goes to Edrak, Enjaz, etc.
    payee_company = models.CharField(max_length=255, blank=True, null=True)

    received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    pending_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    last_updated = models.DateTimeField(auto_now=True)
    is_replaced_partial = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if self.cheque_number and self.cheque_number.strip().upper() == "CASH":
            self.status = "Cleared"

        super().save(*args, **kwargs)
        return f"AED {self.amount} - {self.status} ({self.contract.tenant.full_name})"

# NEW MODEL: Tracks Sync Events
class SyncLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default='Success')

    def __str__(self):
        return f"Sync at {self.timestamp}"


class PropertyDocument(models.Model):
    property_asset = models.ForeignKey('Property', on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='property_docs/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - {self.property_asset.name}"

class UnitDocument(models.Model):
    unit = models.ForeignKey('Unit', on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='unit_docs/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - {self.unit.unit_number}"


class DocumentFolder(models.Model):
    owner = models.ForeignKey(Landlord, on_delete=models.CASCADE, related_name='folders')
    name = models.CharField(max_length=255)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE, related_name='subfolders')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class DocumentFile(models.Model):
    owner = models.ForeignKey('Landlord', on_delete=models.CASCADE, related_name='drive_files')
    folder = models.ForeignKey(DocumentFolder, on_delete=models.CASCADE, related_name='files', null=True, blank=True)
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to='drive_files/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class ApprovalDocument(models.Model):
    landlord = models.ForeignKey(Landlord, on_delete=models.CASCADE, related_name='approvals', null=True, blank=True)
    doc_type = models.CharField(max_length=100, default="Voucher")
    doc_date = models.DateField()
    doc_no = models.CharField(max_length=100)
    credit_account = models.CharField(max_length=255, blank=True, null=True)
    debit_account = models.CharField(max_length=255, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    l1 = models.CharField(max_length=10, default="NO")
    l2 = models.CharField(max_length=10, default="NO")
    l3 = models.CharField(max_length=10, default="NO")
    l4 = models.CharField(max_length=10, default="NO")
    status = models.CharField(max_length=50) # UnApproved, Approved, Rejected
    created_by = models.CharField(max_length=100, blank=True, null=True)
    approved_by = models.CharField(max_length=100, blank=True, null=True)
    comments = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.doc_no} - {self.status}"


class BankDeposit(models.Model):
    voucher_no = models.CharField(max_length=100, unique=True)
    doc_abbr = models.CharField(max_length=10, blank=True, null=True)
    doc_prefix = models.CharField(max_length=10, blank=True, null=True)
    date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    narration = models.TextField(blank=True, null=True)

    # --- THE NEW PACT COLUMNS ---
    tenant = models.CharField(max_length=255, blank=True, null=True)
    unit = models.CharField(max_length=100, blank=True, null=True)
    tower = models.CharField(max_length=255, blank=True, null=True)
    cheque_no = models.CharField(max_length=100, blank=True, null=True)
    cheque_date = models.CharField(max_length=100, blank=True, null=True)
    pact_status_id = models.CharField(max_length=50, blank=True, null=True)
    last_updated = models.DateTimeField(auto_now=True)

    linked_property = models.ForeignKey(Property, on_delete=models.SET_NULL, null=True, blank=True)
    linked_landlord = models.ForeignKey(Landlord, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.voucher_no} - AED {self.amount}"


class ContractLedger(models.Model):
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='true_ledger_entries')
    doc_date = models.DateField()
    doc_no = models.CharField(max_length=100)
    doc_abbr = models.CharField(max_length=20)
    debit_account = models.CharField(max_length=255, blank=True, null=True)
    credit_account = models.CharField(max_length=255, blank=True, null=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_debit = models.BooleanField(default=False)
    particular = models.CharField(max_length=500, null=True, blank=True)
    chq_no = models.CharField(max_length=100, null=True, blank=True)
    chq_date = models.DateField(null=True, blank=True)
    bank = models.CharField(max_length=200, null=True, blank=True)
    tracking_no = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        ordering = ['doc_date', 'id']



# ------------------------------------------------------------------------
# 7. OWNER EXPENSES (GENERAL LEDGER)
# Syncs from ACC_DocDetails where the Owner is the DebitAccount
# ------------------------------------------------------------------------
class OwnerExpense(models.Model):
    landlord = models.ForeignKey(Landlord, on_delete=models.CASCADE, related_name='expenses')
    date = models.DateField()
    voucher_no = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    supplier = models.CharField(max_length=255, blank=True, null=True)
    tower_name = models.CharField(max_length=255, blank=True, null=True)
    unit_name = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    narration = models.TextField(blank=True, null=True)
    is_cleared = models.BooleanField(default=False)
    clearance_voucher_no = models.CharField(max_length=100, null=True, blank=True)
    clearance_narration = models.TextField(null=True, blank=True)
    clearance_amount = models.FloatField(null=True, blank=True)  # 🚀 ADD THIS

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.voucher_no} - AED {self.amount}"



class PropertyAlias(models.Model):
    keyword = models.CharField(max_length=100, unique=True, help_text="The abbreviation or keyword typed by accountants (e.g., 'mybn' or 'helio')")
    target_name = models.CharField(max_length=200, help_text="The exact name of the Property/Villa it should map to (e.g., 'Muhammed Yousuf Bldg')")

    class Meta:
        verbose_name_plural = "Property Aliases"

    def __str__(self):
        return f"{self.keyword} -> {self.target_name}"