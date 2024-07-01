from rest_framework import serializers

from .models import StatusPage


class StatusPageSerializer(serializers.ModelSerializer):
    isPublic = serializers.BooleanField(source="is_public")

    class Meta:
        model = StatusPage
        fields = ("name", "slug", "isPublic", "monitors")
        read_only_fields = ("slug",)
