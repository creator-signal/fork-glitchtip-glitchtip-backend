from django.urls import path

from .views import refresh_license_view, self_host_stripe_webhook_view

urlpatterns = [
    path(
        "self-host-licensing/stripe-webhook/",
        self_host_stripe_webhook_view,
        name="self_host_stripe_webhook",
    ),
    path(
        "api/0/self-host-licensing/refresh/",
        refresh_license_view,
        name="self_host_license_refresh",
    ),
]
