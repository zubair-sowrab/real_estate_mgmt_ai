from django.urls import path
from . import views


urlpatterns = [
    # The new beautiful landing page
    path('', views.landing_page, name='landing_page'),

    # Auth
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Admin Portal (Formerly portal_home)
    path('admin-portal/', views.admin_dashboard, name='admin_dashboard'),

    # Owner Routes
    path('owner/<int:landlord_id>/', views.owner_dashboard, name='owner_dashboard'),
    path('owner/<int:landlord_id>/assets/', views.owner_properties, name='owner_properties'),
    path('owner/<int:landlord_id>/vault/', views.owner_vault, name='owner_vault'),
    # In realestate/urls.py, add these inside urlpatterns:

    path('owner/<int:landlord_id>/view-properties/', views.owner_view_properties, name='owner_view_properties'),
    path('owner/<int:landlord_id>/tenant-details/', views.owner_tenant_details, name='owner_tenant_details'),

    # Endpoint to handle form uploads
    path('owner/<int:landlord_id>/upload-doc/', views.upload_document, name='upload_document'),
path('owner/<int:landlord_id>/drive/', views.owner_drive, name='owner_drive'),
    path('owner/<int:landlord_id>/drive/create-folder/', views.drive_create_folder, name='drive_create_folder'),
    path('owner/<int:landlord_id>/drive/upload-file/', views.drive_upload_file, name='drive_upload_file'),
# In realestate/urls.py, add these to the bottom of the owner block:
    path('owner/<int:landlord_id>/drive/rename/<str:item_type>/<int:item_id>/', views.drive_rename_item, name='drive_rename_item'),
    path('owner/<int:landlord_id>/drive/delete/<str:item_type>/<int:item_id>/', views.drive_delete_item, name='drive_delete_item'),
path('owner/<int:landlord_id>/approvals/', views.owner_approvals, name='owner_approvals'),
    # Endpoint to handle form uploads
    path('owner/<int:landlord_id>/upload-doc/', views.upload_document, name='upload_document'),

    # NEW: Endpoint to handle deleting property documents
    path('owner/<int:landlord_id>/delete-property-doc/<int:doc_id>/', views.delete_property_document,
         name='delete_property_document'),
# Reports Engine
    path('owner/<int:landlord_id>/reports/', views.owner_reports, name='owner_reports'),
path('owner/<int:landlord_id>/occupancy/', views.owner_occupancy_status, name='owner_occupancy_status'),
path('owner/<int:landlord_id>/cashflow/', views.owner_cashflow, name='owner_cashflow'),

path('owner/<int:landlord_id>/expenses/', views.owner_expenses, name='owner_expenses'),
path('owner/<int:landlord_id>/health/', views.tenant_health, name='tenant_health'),
# At the top of urls.py

# Inside your urlpatterns list[cite: 3], add:

path('owner/<int:landlord_id>/reports/rent/', views.report_rent, name='report_rent'),
    path('owner/<int:landlord_id>/reports/pdc/', views.report_pdc, name='report_pdc'),
    path('owner/<int:landlord_id>/reports/compare/', views.report_compare, name='report_compare'),
    path('owner/<int:landlord_id>/reports/expenses/', views.report_expenses, name='report_expenses'),
    path('owner/<int:landlord_id>/reports/tenant/', views.report_tenant, name='report_tenant'),
]