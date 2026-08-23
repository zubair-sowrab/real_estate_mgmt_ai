from django.contrib import admin
from .models import Landlord, Property, Unit, Tenant, Contract, Collection, BankDeposit

# Customize the Admin headers to match your brand
admin.site.site_header = "YBN Real Estate Secure Admin"
admin.site.site_title = "YBN Portal Vault"
admin.site.index_title = "Central Database Management"

# --- INLINES (To see history and relationships on one page) ---
# This is what allows you to see everything connected together comprehensively!

class PropertyInline(admin.TabularInline):
    model = Property
    extra = 0
    show_change_link = True

class UnitInline(admin.TabularInline):
    model = Unit
    extra = 0
    show_change_link = True

class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0
    show_change_link = True

class CollectionInline(admin.TabularInline):
    model = Collection
    extra = 0
    show_change_link = True

# --- MAIN ADMIN CLASSES ---

@admin.register(Landlord)
class LandlordAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_email', 'contact_phone', 'pact_landlord_id')
    search_fields = ('name', 'pact_landlord_id')
    inlines = [PropertyInline] # Instantly see all buildings owned by this landlord

@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'location', 'total_units', 'pact_property_id')
    search_fields = ('name', 'pact_property_id', 'owner__name')
    list_filter = ('owner',)
    inlines = [UnitInline] # See every single unit inside this property

@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ('unit_number', 'property_asset', 'unit_type', 'status', 'base_rent')
    list_filter = ('status', 'unit_type', 'property_asset')
    search_fields = ('unit_number', 'pact_unit_id', 'property_asset__name')
    inlines = [ContractInline] # See the full rental history for this specific apartment

@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'phone', 'emirates_id', 'pact_tenant_id')
    search_fields = ('full_name', 'emirates_id', 'pact_tenant_id', 'phone')
    inlines = [ContractInline] # See all past and present leases for this tenant

@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'unit', 'start_date', 'end_date', 'total_amount', 'status')
    list_filter = ('status', 'start_date', 'end_date')
    search_fields = ('tenant__full_name', 'unit__unit_number', 'pact_contract_id')
    inlines = [CollectionInline] # See every single payment/cheque tied to this contract

@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ('contract', 'amount', 'due_date', 'payment_type', 'status', 'cheque_number')
    list_filter = ('status', 'payment_type', 'due_date')
    search_fields = ('cheque_number', 'pact_transaction_id', 'contract__tenant__full_name')
    date_hierarchy = 'due_date' # Adds a timeline drill-down filter at the top of the page


@admin.register(BankDeposit)
class BankDepositAdmin(admin.ModelAdmin):
    list_display = ('voucher_no', 'date', 'amount', 'doc_abbr', 'tenant', 'cheque_no', 'pact_status_id')
    search_fields = ('voucher_no', 'tenant', 'cheque_no', 'narration')
    list_filter = ('doc_abbr', 'pact_status_id', 'date')
    ordering = ('-date',)



from .models import PropertyAlias

@admin.register(PropertyAlias)
class PropertyAliasAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'target_name')
    search_fields = ('keyword', 'target_name')