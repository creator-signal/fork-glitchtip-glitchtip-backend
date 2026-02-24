from django.contrib import admin

from .models import TransactionGroup


class TransactionGroupAdmin(admin.ModelAdmin):
    search_fields = ["transaction", "op", "project__organization__name"]
    list_display = [
        "transaction",
        "project",
        "op",
        "method",
        "count",
        "avg_duration",
        "p50",
        "p95",
        "error_count",
        "last_seen",
    ]
    list_filter = ["created", "op", "method"]
    autocomplete_fields = ["project"]


admin.site.register(TransactionGroup, TransactionGroupAdmin)
