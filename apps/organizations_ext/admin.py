from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)

from apps.stripe.models import StripeSubscription
from apps.stripe.utils import get_stripe_link

from .models import (
    Organization,
    OrganizationOwner,
    OrganizationSocialApp,
    OrganizationUser,
)
from .resources import OrganizationResource, OrganizationUserResource

ORGANIZATION_LIST_FILTER = (
    "is_active",
    "is_accepting_events",
    "stripesubscription__price__product",
)


class OwnerInline(BaseOwnerInline):
    model = OrganizationOwner


class OrganizationUserInline(admin.StackedInline):
    raw_id_fields = ("user",)
    model = OrganizationUser
    extra = 0


class OrganizationSubscriptionInline(admin.StackedInline):
    model = StripeSubscription
    extra = 0
    readonly_fields = [field.name for field in StripeSubscription._meta.fields]


class GlitchTipBaseOrganizationAdmin(BaseOrganizationAdmin):
    readonly_fields = ("customer_link", "subscription_link", "created")
    list_filter = ORGANIZATION_LIST_FILTER
    inlines = [OrganizationUserInline, OwnerInline, OrganizationSubscriptionInline]
    show_full_result_count = False

    def customer_link(self, obj):
        if customer_id := obj.stripe_customer_id:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                get_stripe_link(customer_id),
                customer_id,
            )

    def subscription_link(self, obj):
        if subscription_id := obj.stripe_primary_subscription_id:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                get_stripe_link(subscription_id),
                subscription_id,
            )


class OrganizationAdmin(GlitchTipBaseOrganizationAdmin, ImportExportModelAdmin):
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
        "stripe_primary_subscription",
    ]
    resource_class = OrganizationResource


class OrganizationSubscription(Organization):
    class Meta:
        proxy = True


class OrganizationSubscriptionAdmin(GlitchTipBaseOrganizationAdmin):
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
        "max_events",
        "current_period_end",
    ]

    def max_events(self, obj):
        if obj.stripe_primary_subscription:
            return obj.stripe_primary_subscription.price.product.events

    def current_period_end(self, obj):
        if obj.stripe_primary_subscription:
            return obj.stripe_primary_subscription.current_period_end

    def get_queryset(self, request):
        qs = Organization.objects.select_related(
            "stripe_primary_subscription__price__product"
        )
        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)
        return qs


class OrganizationUserAdmin(BaseOrganizationUserAdmin, ImportExportModelAdmin):
    list_display = ["user", "organization", "role", "email"]
    search_fields = ("email", "user__email", "organization__name")
    list_filter = ("role",)
    resource_class = OrganizationUserResource


class OrganizationSocialAppAdmin(admin.ModelAdmin):
    list_display = ["organization", "social_app"]
    search_fields = ("organization__name", "social_app__name")


admin.site.register(Organization, OrganizationAdmin)
if settings.BILLING_ENABLED:
    admin.site.register(OrganizationSubscription, OrganizationSubscriptionAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
admin.site.register(OrganizationSocialApp, OrganizationSocialAppAdmin)
