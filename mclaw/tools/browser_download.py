# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Download direct URLs through Chromium while preserving the selected page."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def download_url(context: Any, url: str) -> Iterator[Any]:
    """Keep browser cookies, redirects and streamed downloads in the same context."""
    page = context.new_page()
    client = None
    failure: list[str] = []
    try:
        client = context.new_cdp_session(page)

        def continue_response(event: dict) -> None:
            request_id = event["requestId"]
            try:
                status = event.get("responseStatusCode", 0)
                headers = event.get("responseHeaders", [])
                if not status or (300 <= status < 400 and any(
                    header["name"].lower() == "location" for header in headers
                )):
                    client.send("Fetch.continueRequest", {"requestId": request_id})
                    return
                if not 200 <= status < 300 or status in {204, 205}:
                    failure.append(f"Download URL returned HTTP {status}; no file was saved.")
                    client.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                    return
                modified = []
                disposition = False
                for header in headers:
                    if header["name"].lower() == "content-disposition":
                        # Preserve server filename / filename* parameters.
                        _, separator, parameters = header["value"].partition(";")
                        modified.append({"name": header["name"], "value": "attachment" + separator + parameters})
                        disposition = True
                    else:
                        modified.append(header)
                if not disposition:
                    modified.append({"name": "Content-Disposition", "value": "attachment"})
                # Rewrite headers at the response boundary without reading the
                # file body into Python. Chromium continues its normal download.
                client.send("Fetch.continueResponse", {
                    "requestId": request_id, "responseCode": status, "responseHeaders": modified,
                })
            except Exception as error:
                failure.append(str(error))
                try:
                    client.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
                except Exception:
                    pass  # Cancellation may already have closed this context.

        client.on("Fetch.requestPaused", continue_response)
        client.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"}]})
        with page.expect_download(timeout=10_000) as captured:
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as error:
                if failure:
                    raise RuntimeError(failure[0]) from None
                # A download terminates navigation; it still needs a real event.
                if not any(marker in str(error) for marker in ("Download is starting", "net::ERR_ABORTED")):
                    raise
        yield captured.value
    finally:
        if client is not None:
            try:
                client.detach()
            except Exception:
                pass
        try:
            page.close()
        except Exception:
            pass  # Preserve the original failure when cancellation closes it.
