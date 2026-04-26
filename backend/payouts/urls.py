from django.urls import path

from . import views

urlpatterns = [
    path("merchants", views.list_merchants),
    path("merchants/<uuid:merchant_id>", views.merchant_detail),
    path("merchants/<uuid:merchant_id>/payouts", views.create_payout),
    path("merchants/<uuid:merchant_id>/payouts/list", views.list_payouts),
    # External scheduler hits this every N minutes to process pending payouts.
    # Token-protected. See DEPLOYMENT.md §4.
    path("internal/drain", views.drain),
]
