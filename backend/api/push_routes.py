"""Web Push and persistent notification-centre routes."""
from errors import NotFound, UpstreamUnavailable, ValidationError
from backend.api.contracts import AuthPolicy, Route, json_response


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
        push.unsubscribe(request.require_actor().user_id, request.body.get("endpoint"))
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


def notifications(request):
    import push
    return json_response(push.notification_center(
        request.require_actor().user_id, request.query_value("limit") or 50))


def read_notification(request):
    import push
    result = push.mark_notification_read(
        request.require_actor().user_id, request.params["notification_id"])
    if result is None:
        raise NotFound("notification not found")
    return json_response(result)


def dismiss_notification(request):
    import push
    result = push.dismiss_notification(
        request.require_actor().user_id, request.params["notification_id"])
    if result is None:
        raise NotFound("notification not found")
    return json_response(result)


def read_all_notifications(request):
    import push
    return json_response(push.mark_all_notifications_read(
        request.require_actor().user_id))


def routes():
    return (
        Route("GET", "/api/push/vapid", public_key, name="push-public-key"),
        Route("POST", "/api/push/subscribe", subscribe, name="push-subscribe"),
        Route("POST", "/api/push/unsubscribe", unsubscribe, name="push-unsubscribe"),
        Route("POST", "/api/push/test", test_push, name="push-test"),
        Route("GET", "/api/notifications", notifications, name="notifications-list"),
        Route("POST", "/api/notifications/read-all", read_all_notifications,
              name="notifications-read-all"),
        Route("POST", "/api/notifications/{notification_id}/read", read_notification,
              name="notifications-read"),
        Route("POST", "/api/notifications/{notification_id}/dismiss", dismiss_notification,
              name="notifications-dismiss"),
    )
