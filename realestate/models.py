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
    unit_number = models.CharField(max_length=50)
    unit_type = models.CharField(max_length=100)  # e.g., '2BHK', 'Retail Shop'
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
        ('Cleared', 'Cleared'),
        ('Pending PDC', 'Pending PDC'),
        ('Bounced', 'Bounced'),
        ('Replaced/Partial', 'Replaced/Partial'),
        ('Replaced', 'Replaced'),
        ('Terminated PDC', 'Terminated PDC'),
    ]

    pact_contract_id = models.CharField(max_length=100, unique=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='contracts')
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, related_name='contracts')

    start_date = models.DateField()
    end_date = models.DateField()
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Active')

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

    pact_transaction_id = models.CharField(max_length=100, unique=True)
    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name='collections')

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField()
    payment_type = models.CharField(max_length=50, help_text="e.g., Cheque, Bank Transfer, Cash")
    cheque_number = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending PDC')

    # NEW FIELDS FOR PARTIAL/REPLACED TRACKING
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