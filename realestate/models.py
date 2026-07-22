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

    @property
    def rent_collections(self):
        sorted_cols = sorted(self.collections.all(), key=lambda c: c.due_date)
        rent_items = []

        # VIP Whitelist: Parking and VAT are strictly part of RENT
        rent_whitelist = ['parking', 'vat']
        extra_keywords = ['tasdeeq', 'sewerage', 'security', 'commission', 'deposit', 'admin', 'fee', 'charge']

        for c in sorted_cols:
            part_text = str(c.particular).strip().lower()

            # PACT ID LOGIC: Pure numbers must be '15' for Rent
            if part_text.isdigit():
                if part_text == '15':
                    rent_items.append(c)
                continue

                # VIP CHECK: Parking & VAT force their way into Rent!
            if any(w in part_text for w in rent_whitelist):
                rent_items.append(c)
                continue

            # BLOCK EXTRAS: Tasdeeq, Deposits, etc.
            if any(k in part_text for k in extra_keywords):
                continue

            rent_items.append(c)

        return rent_items

    @property
    def extra_collections(self):
        sorted_cols = sorted(self.collections.all(), key=lambda c: c.due_date)
        extra_items = []

        rent_whitelist = ['parking', 'vat']
        extra_keywords = ['tasdeeq', 'sewerage', 'security', 'commission', 'deposit', 'admin', 'fee', 'charge']

        for c in sorted_cols:
            part_text = str(c.particular).strip().lower()

            if part_text.isdigit():
                if part_text != '15':
                    extra_items.append(c)
                continue

            # Skip Parking & VAT so they aren't placed in Extras
            if any(w in part_text for w in rent_whitelist):
                continue

            if any(k in part_text for k in extra_keywords):
                extra_items.append(c)

        return extra_items

    @property
    def total_paid(self):
        # RESTORED: Replaced and Partial cheques are active credits!
        valid_statuses = ['Cleared', 'Posted', 'Pending PDC', 'Replaced/Partial', 'Replaced']
        return sum(c.amount for c in self.rent_collections if c.status in valid_statuses)

    @property
    def remaining_balance(self):
        bal = self.net_rent_amount - self.total_paid
        return bal if bal > 0 else 0

    @property
    def all_valid_collections(self):
        # RESTORED: Replaced and Partial cheques are active credits!
        valid_statuses = ['Cleared', 'Posted', 'Pending PDC', 'Replaced/Partial', 'Replaced']
        return [c for c in self.collections.all() if c.status in valid_statuses]

    @property
    def total_paid_all(self):
        return sum(c.amount for c in self.all_valid_collections)

    @property
    def remaining_balance_all(self):
        bal = self.total_amount - self.total_paid_all
        return bal if bal > 0 else 0

    @property
    def remaining_balance_all(self):
        bal = self.total_amount - self.total_paid_all
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

    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.voucher_no} - AED {self.amount}"