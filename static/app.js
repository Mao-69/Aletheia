/* =====================================================================
   Library of Aletheia — frontend behaviour.

   No frameworks; pure DOM / fetch. Loaded with `defer`, runs after parse.
   No inline event handlers — every interactive element uses [data-action]
   so CSP can keep `script-src` strict. Click delegation lives in init().

   View architecture
   -----------------
   The page has multiple <section class="view"> elements. At any time,
   exactly one is visible (others have [hidden]). showView(id) flips
   visibility and focuses the first input/button. The router is just
   functions calling showView; there's no URL routing.

   Reader rendering
   ----------------
   Server returns 3,200-character `content` per page. We split it into 40
   lines × 80 chars and render via textContent (never innerHTML — content
   is user-derived). The highlight overlay is positioned in `ch` units so
   it stays aligned with the monospace grid at any font size.
   ===================================================================== */

(function () {
    "use strict";

    // -------------------------------------------------------------- CONSTANTS
    const PAGE_LINES = 40;
    const PAGE_COLS  = 80;

    // -------------------------------------------------------------- DOM HELPERS

    const $ = (sel, root = document) => root.querySelector(sel);
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    // -------------------------------------------------------------- STATE

    /** The page currently loaded in the reader. Null when reader is empty. */
    let currentPage = null;

    /** The most recent /encode result, used by the results view's "save QR" / "read" actions. */
    let lastEncodeResult = null;

    /** Cached scanner instance (html5-qrcode). Lazy-built. */
    let scanner = null;

    // -------------------------------------------------------------- TOAST

    const toastEl = $("#toast");
    let toastTimer = null;

    function toast(message, kind = "info") {
        if (toastTimer) clearTimeout(toastTimer);
        toastEl.textContent = message;
        toastEl.classList.remove("toast-error", "toast-success");
        if (kind === "error") toastEl.classList.add("toast-error");
        if (kind === "success") toastEl.classList.add("toast-success");
        toastEl.classList.add("toast-visible");
        toastTimer = setTimeout(() => {
            toastEl.classList.remove("toast-visible");
        }, kind === "error" ? 5000 : 3000);
    }

    // -------------------------------------------------------------- VIEW SWITCH

    function showView(id) {
        $$(".view").forEach((v) => {
            v.hidden = (v.id !== id);
        });
        // Focus management: send keyboard users to the first reasonable target.
        const view = $("#" + id);
        if (!view) return;
        const target = view.querySelector("textarea, button.primary-btn, [data-action='do-random'], .menu-tile");
        if (target) target.focus({ preventScroll: false });
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    // -------------------------------------------------------------- API

    async function api(path, formData) {
        const opts = { method: "POST" };
        if (formData) opts.body = formData;

        let response;
        try {
            response = await fetch(path, opts);
        } catch (e) {
            throw new Error("Network error — is the server running?");
        }

        let body = null;
        try {
            body = await response.json();
        } catch (e) {
            // empty/non-JSON response
        }

        if (!response.ok) {
            const detail = (body && body.detail) || `Server returned ${response.status}`;
            throw new Error(detail);
        }
        return body;
    }

    // -------------------------------------------------------------- ACTIONS

    async function submitCreate(form) {
        const fd = new FormData(form);
        const submitBtn = form.querySelector("button[type=submit]");
        submitBtn.disabled = true;
        submitBtn.textContent = "Inscribing…";
        try {
            const res = await api("/encode", fd);
            lastEncodeResult = res;
            renderResults(res);
            showView("view-results");
        } catch (e) {
            toast(e.message, "error");
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = "Inscribe ⇢";
        }
    }

    async function submitSearch(form) {
        const fd = new FormData(form);
        const submitBtn = form.querySelector("button[type=submit]");
        submitBtn.disabled = true;
        submitBtn.textContent = "Locating…";
        try {
            const res = await api("/search", fd);
            // Search → go straight to reader. We need to fetch the page contents:
            const browseFd = new FormData();
            browseFd.append("identifier", res.compact_identifier);
            const page = await api("/browse", browseFd);
            // Apply the search highlight (search returned [start_line, start_col, end_line, end_col]).
            page.highlight = res.highlight || null;
            currentPage = page;
            renderReader(page);
            showView("view-reader");
        } catch (e) {
            toast(e.message, "error");
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = "Locate ⇢";
        }
    }

    async function submitBrowse(form) {
        const fd = new FormData(form);
        const submitBtn = form.querySelector("button[type=submit]");
        submitBtn.disabled = true;
        submitBtn.textContent = "Opening…";
        try {
            const page = await api("/browse", fd);
            currentPage = page;
            renderReader(page);
            showView("view-reader");
        } catch (e) {
            toast(e.message, "error");
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = "Open ⇢";
        }
    }

    async function doRandom() {
        try {
            const page = await api("/random", new FormData());
            currentPage = page;
            renderReader(page);
            showView("view-reader");
        } catch (e) {
            toast(e.message, "error");
        }
    }

    async function navigatePage(direction) {
        if (!currentPage) return;
        const target = direction === "next" ? currentPage.next_compact : currentPage.prev_compact;
        if (!target) return;
        const fd = new FormData();
        fd.append("identifier", target);
        try {
            const page = await api("/browse", fd);
            currentPage = page;
            renderReader(page);
            window.scrollTo({ top: 0, behavior: "smooth" });
        } catch (e) {
            toast(e.message, "error");
        }
    }

    // -------------------------------------------------------------- RENDER: results

    function renderResults(res) {
        const compactEl = $("#results-compact");
        compactEl.textContent = res.compact_identifier;
        $("#results-page").textContent = res.page;
        $("#results-full-meta").textContent =
            `${res.identifier.length.toLocaleString()} characters (full identifier)`;

        const qrEl = $("#results-qr");
        const warning = $("#results-qr-warning");
        if (res.qr_base64) {
            qrEl.src = "data:image/png;base64," + res.qr_base64;
            qrEl.hidden = false;
            warning.hidden = true;
        } else {
            qrEl.hidden = true;
            warning.hidden = false;
        }
    }

    // -------------------------------------------------------------- RENDER: reader

    function renderReader(page) {
        // Split 3,200 chars into 40 lines of 80. textContent only — never innerHTML.
        // The server already pads to exactly PAGE_LENGTH; trim/pad just in case.
        let text = page.content || "";
        if (text.length < PAGE_LINES * PAGE_COLS) {
            text = text + " ".repeat(PAGE_LINES * PAGE_COLS - text.length);
        } else if (text.length > PAGE_LINES * PAGE_COLS) {
            text = text.slice(0, PAGE_LINES * PAGE_COLS);
        }

        const lines = [];
        for (let i = 0; i < PAGE_LINES; i++) {
            lines.push(text.slice(i * PAGE_COLS, (i + 1) * PAGE_COLS));
        }
        $("#reader-page").textContent = lines.join("\n");

        // Header metadata
        $("#reader-room").textContent = page.room_short || page.room || "";
        $("#reader-pos").textContent =
            `wall ${page.wall} · shelf ${page.shelf} · book ${page.book} · page ${page.page}`;
        $("#reader-pageno").textContent = `page ${page.page} / 410`;

        // Disable nav buttons we can't satisfy (shouldn't happen in babel — every
        // page has a prev/next — but guard against missing fields).
        $("#reader-prev").disabled = !page.prev_compact;
        $("#reader-next").disabled = !page.next_compact;

        renderHighlight(page.highlight);
    }

    function renderHighlight(highlight) {
        const overlay = $("#reader-highlight");
        if (!highlight || highlight.length !== 4) {
            overlay.hidden = true;
            return;
        }

        // Measure the page's character cell so we can position the overlay
        // exactly. We measure on the rendered <pre> so any font-size or
        // line-height changes from media queries are picked up automatically.
        const pre = $("#reader-page");
        const frame = $("#reader-page-frame");
        const preRect = pre.getBoundingClientRect();
        const frameRect = frame.getBoundingClientRect();

        // Width of one character: take the full width of the line and divide.
        // (The line is exactly 80 chars wide in monospace.)
        const charWidth = preRect.width / PAGE_COLS;
        const computed = window.getComputedStyle(pre);
        const fontSize = parseFloat(computed.fontSize) || 14;
        const lineHeight = parseFloat(computed.lineHeight) || (fontSize * 1.4);

        const [startLine, startCol, endLine, endCol] = highlight;

        // Offset of the <pre> within the frame (frame has padding, pre starts
        // inside it). Using bounding rects accounts for the frame's padding,
        // any scroll position, etc.
        const offsetX = preRect.left - frameRect.left;
        const offsetY = preRect.top - frameRect.top;

        if (startLine === endLine) {
            // Single-line highlight: simple box.
            overlay.style.left = (offsetX + startCol * charWidth) + "px";
            overlay.style.top = (offsetY + startLine * lineHeight) + "px";
            overlay.style.width = ((endCol - startCol) * charWidth) + "px";
            overlay.style.height = lineHeight + "px";
        } else {
            // Multi-line: span from start of first line to end of last line.
            // We render a rectangle covering all rows; for the typical use case
            // (a phrase wrapping to the next line) this looks fine.
            overlay.style.left = (offsetX + 0) + "px";
            overlay.style.top = (offsetY + startLine * lineHeight) + "px";
            overlay.style.width = (PAGE_COLS * charWidth) + "px";
            overlay.style.height = ((endLine - startLine + 1) * lineHeight) + "px";
        }
        overlay.hidden = false;
    }

    // -------------------------------------------------------------- COPY/SAVE

    async function copyText(text, label) {
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            toast(`${label} copied`, "success");
        } catch (e) {
            // Fallback: select-all the element and prompt manual copy.
            toast("Couldn't copy — try selecting the text manually.", "error");
        }
    }

    function saveQrFromCurrent() {
        // Pull the QR image from the results view (most recently encoded).
        const img = $("#results-qr");
        if (!img.src || img.hidden) {
            toast("No QR to save.", "error");
            return;
        }
        const a = document.createElement("a");
        a.href = img.src;
        const idForName = (lastEncodeResult && lastEncodeResult.compact_identifier) || "aletheia";
        a.download = `aletheia-${idForName.replace(/[^a-zA-Z0-9]+/g, "-")}.png`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    function saveQrFromDialog() {
        const img = $("#qr-dialog-img");
        if (!img.src) return;
        const a = document.createElement("a");
        a.href = img.src;
        const idForName = (currentPage && currentPage.compact_identifier) || "aletheia";
        a.download = `aletheia-${idForName.replace(/[^a-zA-Z0-9]+/g, "-")}.png`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    // -------------------------------------------------------------- QR FOR CURRENT PAGE

    async function showQrForCurrent() {
        if (!currentPage) return;
        const compactId = currentPage.compact_identifier;
        if (!compactId) return;

        const dialog = $("#qr-dialog");
        const img = $("#qr-dialog-img");
        const caption = $("#qr-dialog-caption");

        // Show the dialog immediately with a placeholder so it feels responsive,
        // then swap in the QR image when the server finishes rendering it.
        img.hidden = true;
        caption.textContent = "Rendering QR…";
        dialog.showModal();

        const fd = new FormData();
        fd.append("identifier", compactId);
        try {
            const res = await api("/qr", fd);
            img.src = "data:image/png;base64," + res.qr_base64;
            img.hidden = false;
            caption.textContent = compactId;
        } catch (e) {
            img.hidden = true;
            caption.textContent = "Couldn't render QR: " + e.message;
        }
    }

    // -------------------------------------------------------------- SCANNER

    async function openScanner() {
        const dialog = $("#scanner-dialog");
        const status = $("#scanner-status");

        // The library is bundled at /static/html5-qrcode.min.js and loaded with
        // `defer`, so it should be ready by the time any user interaction fires.
        // If it isn't here, something prevented the file from loading entirely
        // (most likely: the file is missing from /static, an aggressive ad-blocker
        // matched on the filename, or a misconfigured reverse proxy).
        if (typeof Html5Qrcode === "undefined") {
            toast(
                "Scanner library didn't load. Check that " +
                "/static/html5-qrcode.min.js is reachable.",
                "error"
            );
            return;
        }

        dialog.showModal();
        status.textContent = "Requesting camera…";

        try {
            if (!scanner) scanner = new Html5Qrcode("scanner-mount");
            await scanner.start(
                { facingMode: "environment" },
                { fps: 10, qrbox: { width: 240, height: 240 } },
                (decodedText) => {
                    closeScanner().then(() => handleScannedIdentifier(decodedText));
                },
                () => {
                    /* per-frame failure callback — silent, normal */
                }
            );
            status.textContent = "Aim camera at QR.";
        } catch (e) {
            status.textContent = "Camera unavailable: " + (e.message || e);
        }
    }

    async function closeScanner() {
        const dialog = $("#scanner-dialog");
        try {
            if (scanner && scanner.isScanning) await scanner.stop();
        } catch (e) {
            // ignore — best-effort cleanup
        }
        if (dialog.open) dialog.close();
    }

    async function handleScannedIdentifier(text) {
        const trimmed = (text || "").trim();
        if (!trimmed) {
            toast("Empty QR.", "error");
            return;
        }
        // Browse with the scanned identifier (full or compact form, both work).
        const fd = new FormData();
        fd.append("identifier", trimmed);
        try {
            const page = await api("/browse", fd);
            currentPage = page;
            renderReader(page);
            showView("view-reader");
            toast("Page opened from QR.", "success");
        } catch (e) {
            toast(e.message, "error");
        }
    }

    // -------------------------------------------------------------- ACTIONS MAP

    const ACTIONS = {
        "goto-menu":               () => showView("view-menu"),
        "goto-create":             () => showView("view-create"),
        "goto-search":             () => showView("view-search"),
        "goto-browse":             () => showView("view-browse"),
        "do-random":               () => doRandom(),
        "open-scanner":            () => openScanner(),
        "close-scanner":           () => closeScanner(),
        "copy-compact":            () => copyText($("#results-compact").textContent, "Compact identifier"),
        "copy-full":                () => copyText(lastEncodeResult && lastEncodeResult.identifier, "Full identifier"),
        "save-qr":                 () => saveQrFromCurrent(),
        "save-qr-from-dialog":     () => saveQrFromDialog(),
        "close-qr-dialog":         () => $("#qr-dialog").close(),
        "open-from-results":       () => {
            // Open the just-encoded page in the reader.
            if (!lastEncodeResult) return;
            const fd = new FormData();
            fd.append("identifier", lastEncodeResult.compact_identifier);
            api("/browse", fd).then((page) => {
                // Apply the highlight from the encode if present (chars/words modes).
                page.highlight = lastEncodeResult.highlight || null;
                currentPage = page;
                renderReader(page);
                showView("view-reader");
            }).catch((e) => toast(e.message, "error"));
        },
        "copy-compact-from-reader": () => copyText(currentPage && currentPage.compact_identifier, "Identifier"),
        "qr-from-reader":           () => showQrForCurrent(),
        "prev-page":                () => navigatePage("prev"),
        "next-page":                () => navigatePage("next"),
    };

    // -------------------------------------------------------------- INIT

    function init() {
        // Centralised click delegation: any element with [data-action] dispatches here.
        document.addEventListener("click", (ev) => {
            const t = ev.target.closest("[data-action]");
            if (!t) return;
            const action = t.getAttribute("data-action");
            const fn = ACTIONS[action];
            if (fn) fn();
        });

        // Form submit handlers (avoiding default GET-with-querystring).
        $("#form-create").addEventListener("submit", (ev) => {
            ev.preventDefault();
            submitCreate(ev.target);
        });
        $("#form-search").addEventListener("submit", (ev) => {
            ev.preventDefault();
            submitSearch(ev.target);
        });
        $("#form-browse").addEventListener("submit", (ev) => {
            ev.preventDefault();
            submitBrowse(ev.target);
        });

        // Highlight follows window resizes (font size changes via media queries).
        window.addEventListener("resize", () => {
            if (currentPage) renderHighlight(currentPage.highlight);
        });

        // Allow Escape to close any open <dialog> (browsers do this for free,
        // but ensure scanner is fully stopped on close).
        $("#scanner-dialog").addEventListener("close", () => {
            if (scanner && scanner.isScanning) {
                scanner.stop().catch(() => {});
            }
        });

        showView("view-menu");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
