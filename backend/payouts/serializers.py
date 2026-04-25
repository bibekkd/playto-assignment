from rest_framework import serializers

from .models import Payout


class PayoutCreateSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.UUIDField()


class PayoutReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payout
        fields = [
            "id",
            "merchant",
            "amount_paise",
            "bank_account_id",
            "status",
            "attempts",
            "last_attempt_at",
            "created_at",
            "updated_at",
        ]
