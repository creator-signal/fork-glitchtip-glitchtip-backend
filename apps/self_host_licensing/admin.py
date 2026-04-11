from django.contrib import admin

from .models import IssuedLicense


@admin.register(IssuedLicense)
class IssuedLicenseAdmin(admin.ModelAdmin):
    list_display = (
        "stripe_subscription_id",
        "email",
        "plan",
        "status",
        "current_period_end",
        "last_issued_at",
    )
    search_fields = ("stripe_subscription_id", "stripe_customer_id", "email")
    readonly_fields = ("created", "last_issued_at")
