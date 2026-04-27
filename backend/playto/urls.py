from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    """Tiny liveness endpoint at `/`.

    Returns 200 + JSON so that hitting the bare hostname (Render dashboard,
    the GitHub Actions workflow, a curl smoke test) gets a clear "yes,
    this service is up" answer instead of a Django 404 page.
    """
    return JsonResponse({"ok": True, "service": "playto-payout"})


urlpatterns = [
    path("", health),
    path("api/v1/", include("payouts.urls")),
]
