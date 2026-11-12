"""Sanitized, finite authentication error vocabulary shared by API tools."""

AUTH_ERROR_CATEGORIES = {
    # Official POST /session examples: 400, 401, and 429 respectively.
    # Source: https://open-api.capital.com/ (embedded OpenAPI response examples).
    "error.null.api.key": "missing_api_key",
    "error.invalid.details": "invalid_login_details",
    "error.too-many.requests": "rate_limited",
    # Retained compatibility entries; not all appear in the current examples.
    "error.invalid.api.key": "invalid_api_key",
    "error.api.key.expired": "expired_api_key",
    "error.public-api.invalid.api.key": "invalid_api_key",
    "error.public-api.invalid.credentials": "invalid_login_details",
    "error.public-api.invalid.password": "invalid_login_details",
    "error.public-api.invalid.identifier": "invalid_login_details",
    "error.public-api.api.key.expired": "expired_api_key",
    "error.public-api.api.key.disabled": "disabled_api_key",
    "error.public-api.exceeded.rate.limit": "rate_limited",
}

def sanitized_auth_error(payload):
    """Expose only recognized fixed error codes, never arbitrary server text."""
    code = payload.get("errorCode") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and code in AUTH_ERROR_CATEGORIES else "unclassified_authentication_error"

def authentication_error_details(response):
    """Record a finite diagnostic vocabulary, excluding arbitrary headers/bodies."""
    content_type = response.headers.get("Content-Type", "")
    media = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
    content_kind = ({"application/json": "json", "text/html": "html", "text/plain": "text"}.get(media)
                    or ("other" if media else "missing"))
    challenge = response.headers.get("WWW-Authenticate", "")
    scheme = challenge.split(None, 1)[0].lower() if isinstance(challenge, str) and challenge.strip() else ""
    challenge_kind = scheme if scheme in ("basic", "bearer", "digest") else "other" if scheme else "absent"
    encoding = response.headers.get("Content-Encoding", "")
    encoding = encoding.strip().lower() if isinstance(encoding, str) else ""
    encoding_kind = encoding if encoding in ("identity", "gzip", "deflate", "br") else "other" if encoding else "absent"
    details = {"error_code": "unclassified_authentication_error", "error_content_type": content_kind,
               "www_authenticate_scheme": challenge_kind, "content_encoding": encoding_kind}
    try:
        payload = response.json()
    except ValueError:
        return {**details, "error_category": "non_json_response", "error_response_format": "non_json",
                "error_code_state": "unparseable"}
    if not isinstance(payload, dict):
        return {**details, "error_category": "unexpected_json_shape", "error_response_format": "json_non_object",
                "error_code_state": "missing"}
    code = sanitized_auth_error(payload)
    if code in AUTH_ERROR_CATEGORIES:
        return {**details, "error_code": code, "error_category": AUTH_ERROR_CATEGORIES[code],
                "error_response_format": "json_object", "error_code_state": "recognized"}
    state = ("missing" if "errorCode" not in payload else
             "unknown" if isinstance(payload["errorCode"], str) else "invalid_type")
    return {**details, "error_category": {"missing": "missing_error_code", "unknown": "unknown_error_code",
                                         "invalid_type": "invalid_error_code_type"}[state],
            "error_response_format": "json_object", "error_code_state": state}
