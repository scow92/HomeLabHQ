"""Web Push routes; failures are deliberately converted to safe API errors."""
from errors import UpstreamUnavailable, ValidationError
from backend.http.router import AuthPolicy, Route
from backend.http.responses import json_response


def public_key(request):
    try:
        import push
        return json_response({"publicKey": push.public_key()})
    except Exception as error:
        raise UpstreamUnavailable("push unavailable") from error


def subscribe(request):
    try:
        import push
        push.subscribe(request.require_actor().user_id, request.body.get("subscription"))
    except Exception as error:
        raise ValidationError("invalid push subscription") from error
    return json_response({"ok": True})


def unsubscribe(request):
    try:
        import push
        push.unsubscribe(request.body.get("endpoint"))
    except Exception as error:
        raise ValidationError("invalid push subscription") from error
    return json_response({"ok": True})


def test_push(request):
    try:
        import push
        result = push.notify({request.require_actor().user_id}, "HomelabHQ test",
                             "Push notifications are working.")
    except Exception as error:
        raise UpstreamUnavailable("push unavailable") from error
    return json_response(result)


def routes():
    return (
        Route("GET", "/api/push/vapid", public_key, name="push-public-key"),
        Route("POST", "/api/push/subscribe", subscribe, name="push-subscribe"),
        Route("POST", "/api/push/unsubscribe", unsubscribe, name="push-unsubscribe"),
        Route("POST", "/api/push/test", test_push, name="push-test"),
    )
