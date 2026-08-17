/**
 * MiniMax References Manager — native widgets (openrouter_api_key, model) stay on top,
 * untouched; everything custom lives in one DOM block BELOW them. Per the visual
 * spec the DOM/canvas split is VISIBLE: no wrapping panel, buttons and prompt sit
 * directly on ComfyUI's grey node body, and the canvas is a BLACK slab that
 * announces "this is the reference area":
 *   [⬆ Image] [⬆ Video] [⬆ Audio] [Save config] [Load config] · · · · · · · [⚙]
 *   <canvas> — black; Images (N/9) / Videos (N/3) / Audio (N/3), three fixed
 *              sections separated by drawn divider lines, a "+" add slot per row
 *   Prompt — one large plain textarea (bound to the hidden `direction` widget)
 * The ⚙ opens a modal for the `system_prompt` widget — never drawn on the node body,
 * only editable behind that modal (see openSystemPromptModal).
 *
 * FIXED SIZE, per spec: "I would like this node to have a fixed size that cannot be
 * moved" / "I don't want the canvas to change at all — always the same row. It just
 * adds or removes content." So: node.resizable = false, and onResize / setSize /
 * computeSize all clamp to fixedSize() regardless of input — a restored workflow, a
 * paste, or a frontend quirk cannot put the node at another size. The three rows are
 * always drawn at full tile height whether they hold 0 or 9 items; only their
 * CONTENTS change. Every piece of machinery that existed to cope with a changing
 * size (content measurement, resize-driven relayout, width floors, the
 * restored-size protection) is deleted — the geometry below is arithmetic over
 * constants, not measurement.
 *
 * RENDERING: the reference rows are a single <canvas>, not per-cell DOM — the LTX
 * Director hybrid (ltx_director.js draws its timeline onto one canvas at :2703 but
 * keeps buttons and the prompt as real DOM). Per-cell DOM kept producing
 * overlap/leakage bugs on this frontend; one drawn surface has nothing to overlap.
 * Everything inside the rows — headers, thumbnails, tag badges, delete, the
 * soundtrack toggle, the add slots, play glyphs, selection — is painted by draw(),
 * the single entry point. No requestAnimationFrame loop: scheduleDraw() coalesces
 * change events (refs, selection, thumb load, probe answer, preview state, the
 * canvas's one real mount via a ResizeObserver) into at most one paint per frame,
 * and draw() never calls setSize/computeSize, so paint can't recurse into layout.
 *
 * PLAYABLE PREVIEWS: click the drawn ▶ on a video or audio tile to play it, click
 * again (or click the playing video) to stop. ONE shared <video> overlay and ONE
 * shared <audio> element per node — never per-cell DOM. The overlay is a child of
 * the block, so ComfyUI's zoom transform applies to it automatically; positioning
 * is plain CSS-px math off the tile rect. Clicking a tile anywhere OUTSIDE the ▶
 * selects it, exactly as before.
 *
 * HIDING `references_json`, `direction` and `system_prompt`: mirrors WhatDreamsCost
 * multi_image_loader.js's hide-widget pattern (:122-146) — a plain `w.hidden = true`
 * gets silently overwritten on V3 (vueNodesMode), which re-derives widget visibility
 * from `hidden`/`type` on every reactive redraw, and a widget left with its native
 * computeSize reserves real layout height: three stacked, that read as a thin bar
 * overlapping our controls. The fix: Object.defineProperty-lock `hidden`/`type` as
 * always-on getters V3's own writes can't override, set computeSize = [0,0]
 * unconditionally, and poll every 50ms for 1s for `w.element` — V3 creates the DOM
 * element backing a multiline STRING widget ASYNCHRONOUSLY, so a one-shot
 * display:none misses it. Every property hideWidget() touches is try/catch-wrapped:
 * a prior version of this file assigned `domWidget.node = node` directly, which is a
 * getter-only accessor on the V3 frontend's BaseWidget and threw a TypeError that
 * aborted loading ANY workflow containing this node — fatal, not cosmetic. That
 * assignment is gone (the node is captured via closure instead); hideWidget()'s own
 * property writes are defensive against the same class of bug.
 *
 * Each video tile carries a soundtrack toggle (♪, bottom-left) — it's the only thing
 * that routes that video's audio to its index-matched video_audio_N output, so it's
 * real functionality, not decoration. Gated on GET /minimax_refpack/probe?file=
 * (media.py's has_audio): dim/pending until the probe answers, permanently disabled
 * for a clip with no audio track. See probeHasAudio()/syncProbes() and drawTile().
 *
 * NOT SURFACED ON THE NODE BODY: the system prompt itself — lives only behind the ⚙
 * modal (openSystemPromptModal), never drawn on the body.
 *
 * CROP + TRIM (openEditModal): one modal edits both. Entry points: the scissors chip
 * in a tile's TOP-LEFT corner (the only free corner — delete owns top-right, ♪ sits
 * bottom-left above the badge, ▶ is centred) and a double-click anywhere on the tile.
 * The modal shows the REAL media via /view (fileUrl) — an <img> for stills, a <video>
 * for clips — with a draggable fraction-space crop rect + corner handles and aspect
 * presets; video/audio get in/out trim handles on a bar plus 2dp second fields. Each
 * row ends in a right-aligned Clear button ("Clear crop" / "Clear trim") that resets
 * that edit — dimmed when there is nothing to clear, so the modal states at a glance
 * whether the reference carries an edit; clearing the crop also releases the aspect
 * lock and unhighlights every preset. No preset is highlighted on open — the highlight
 * means the user chose a lock, not the free default. Save
 * writes crop ([x,y,w,h] fractions) / trim ([start,end] seconds) onto the reference —
 * refs.py validates and media.py applies them — and drops the file's thumbCache entry
 * so the tile redraws from the thumb route with the edit baked in. The scissors chip
 * inverts to a light chip while an edit is set (same signalling as ♪), the badge grows
 * a third line ("2.00-6.50s · cropped"), and the click-to-play preview plays only the
 * trimmed span (seek to the in-point, timeupdate stops it at the out-point). All of it
 * keeps the module invariants: no per-cell DOM, one shared <video>/<audio> per node,
 * scheduleDraw coalescing, no requestAnimationFrame loop.
 */

import { app } from "../../scripts/app.js";

const NODE_NAME = "MiniMaxH3ReferencePack";

// ---------------------------------------------------------------------------
// 0.3.1 -> 0.3.2 widget migration.
//
// `use_openrouter` was a BOOLEAN. `prompt_provider` is a combo in the SAME slot,
// because appending it instead would have re-pointed every widget after it - the
// exact class of bug 0.3.1 shipped and fixed. litegraph restores widgets_values
// positionally and does not type-check, so a workflow saved at 0.3.1 hands the
// combo a raw `true`, which then fails validation at queue time.
//
// configure() applies widgets_values BEFORE calling onConfigure, so by the time we
// run the wrong value is already sitting on the widget and this is a repair rather
// than an interception. That is fine and is why it is written this way: one place,
// after the fact, no hooking of litegraph internals.
//
// The block between the two MMRP-MIGRATE markers is extracted and executed by
// tests/test_migration.py under node. Keep the markers, and keep this function
// free of imports and DOM access so it stays runnable in isolation.
// >>> MMRP-MIGRATE
const PROVIDER_VALUES = ["openrouter", "local", "none"];

function migrateProviderValue(raw) {
    // Mirrors endpoint.normalize_provider in Python. Both exist on purpose: this one
    // fixes the graph the user is looking at, the Python one catches an API client
    // that never loaded a browser. Total by design - an unknown value resolves to the
    // default rather than throwing, because a broken workflow that will not queue is
    // worse than a workflow that quietly writes its prompt the old way.
    if (raw === true) return "openrouter";
    if (raw === false) return "none";
    const text = String(raw ?? "").trim().toLowerCase();
    if (PROVIDER_VALUES.includes(text)) return text;
    if (text === "true") return "openrouter";
    if (text === "false") return "none";
    return "openrouter";
}

function migrateProviderWidget(widget) {
    // Returns true when it changed something, so the caller can log it once.
    if (!widget) return false;
    const migrated = migrateProviderValue(widget.value);
    if (widget.value === migrated) return false;
    widget.value = migrated;
    return true;
}

// --- 0.3.3 reorder -------------------------------------------------------------
//
// widgets_values is a positional array, so the declaration order in nodes.py is a wire
// format. 0.3.3 regrouped the widgets by decision flow, which means every array saved
// before it now decodes into the wrong widgets - width into prompt_provider, and so on.
//
// Detection is by VALUE SHAPE rather than by properties.ver. A graph can reach us
// without a ver (hand-edited, older frontend, copied node), and being wrong here is
// worse than the bug it fixes: it would silently scramble a workflow that was fine.
// Length plus the type at the provider slot identifies each layout unambiguously.
const ORDER_0_3_1 = [
    "direction", "openrouter_api_key", "openrouter_model", "references_json",
    "system_prompt", "width", "height", "length_seconds", "prompt_provider",
    "reasoning_effort", "job_type", "max_reference_edge",
];
const ORDER_0_3_2 = ORDER_0_3_1.concat(["api_base", "local_model_slug"]);
const ORDER_0_3_3 = [
    "direction", "references_json", "system_prompt", "prompt_provider",
    "openrouter_api_key", "openrouter_model", "reasoning_effort", "api_base",
    "local_model_slug", "job_type", "width", "height", "length_seconds",
    "max_reference_edge",
];

function detectLayout(values) {
    if (!Array.isArray(values)) return null;
    // 0.3.3 already: slot 3 holds a provider string.
    if (PROVIDER_VALUES.includes(String(values[3] ?? "").trim().toLowerCase())) {
        return ORDER_0_3_3;
    }
    // 0.3.1: use_openrouter was a boolean at slot 8. 0.3.2: a provider string there.
    const slot8 = values[8];
    if (typeof slot8 === "boolean") return ORDER_0_3_1;
    if (PROVIDER_VALUES.includes(String(slot8 ?? "").trim().toLowerCase())) {
        return values.length > 12 ? ORDER_0_3_2 : ORDER_0_3_1;
    }
    return null;   // unrecognised: leave it alone rather than guess
}

function remapWidgetValues(values) {
    // -> {name: value} for whatever layout this array was written in, or null if the
    // array is not one we recognise. Names absent from the old layout simply do not
    // appear, so the caller leaves those widgets at their defaults.
    const order = detectLayout(values);
    if (!order) return null;
    const out = {};
    for (let i = 0; i < order.length && i < values.length; i++) {
        out[order[i]] = values[i];
    }
    // The old `model` slot is this pack's openrouter_model; the old generic override is
    // the local slug. Both renames happened in 0.3.3 alongside the reorder.
    if (out.model !== undefined && out.openrouter_model === undefined) {
        out.openrouter_model = out.model;
    }
    if (out.model_override !== undefined && out.local_model_slug === undefined) {
        out.local_model_slug = out.model_override;
    }
    if (out.prompt_provider !== undefined) {
        out.prompt_provider = migrateProviderValue(out.prompt_provider);
    }
    return out;
}
// <<< MMRP-MIGRATE
const KINDS = ["image", "video", "audio"];
const CAPS = { image: 9, video: 3, audio: 3 };
const SECTION_LABEL = { image: "Images", video: "Videos", audio: "Audio" };

// LTX Director's flat neutral palette (WhatDreamsCost js/ltx_director.js), counted
// out of that file, not invented here. The canvas reads these directly; refpack.css
// carries the DOM half (buttons, prompt box, modal). The wells are nudged up from
// the reference's #111/#121212: those values assume a #1e1e1e panel behind them,
// and this canvas is BLACK — at #111 a tile well is 17/255 off its background,
// structurally present but invisible (same class of contrast bug as the prompt box).
const C = {
    well: "#1a1a1a",
    wellDeep: "#141414",
    surface: "#2a2a2a",
    raised: "#333",
    border: "#444",
    text: "#e0e0e0",
    textMuted: "#aaa",
    textFaint: "#888",
    textDim: "#666",
    danger: "#ff4444",
};

// Canvas row geometry, all in CSS px (draw() scales the backing store by
// devicePixelRatio, so layout math never sees physical pixels). Tile size started
// as LTX Director's 75px gallery cell (multi_image_loader.js:208), scaled 1.75×
// per spec ("make them about 1.75 times the current size") — the on-tile furniture
// (badge, delete, ♪, ▶) is scaled with it so the proportions hold.
const CL = {
    x0: 10, // inner side padding of the black slab (also clears the selection stroke)
    padTop: 8,
    headerH: 15,
    headGap: 12, // 8px of real air between a section label and its strip (UI review #7)
    tile: 131, // 75 × 1.75
    gap: 6,
    stripPad: 3, // above/below tiles, room for the selection stroke
    // Generous per spec: three DISTINCT sections, divider drawn at the midpoint
    // of this gap (see draw()).
    rowGap: 18,
    // On-tile furniture, scaled with the tile.
    del: 20, // delete chip circle DIAMETER — a quiet chip, not red (UI review #5)
    sound: 28, // soundtrack toggle box
    playR: 14, // play glyph circle radius — r20 buried a third of the thumbnail
    badge1: 18, // tag badge height, one line (11px type)
    badge2: 30, // tag badge height, two lines
    badge3: 42, // tag badge height, three lines (tag + vid_audio + the crop/trim line)
    // The per-row add affordance: an icon-only square, NOT a toolbar-button clone
    // (UI review #1). Fixed placement — always where the next tile would go, with
    // a 24px gap separating it from the reference group so it doesn't read as
    // another tile; it never centres itself and only ever moves rightwards.
    addBtn: 44,
    addGap: 24,
};
CL.stripH = CL.stripPad * 2 + CL.tile;

// The rows never move: every section is always drawn at full tile height, 0 items or
// 9. Static layout, computed once — this is the whole point of the fixed-size node.
const CANVAS_ROWS = (() => {
    const rows = [];
    let y = CL.padTop;
    for (const kind of KINDS) {
        rows.push({ kind, y, stripY: y + CL.headerH + CL.headGap });
        y += CL.headerH + CL.headGap + CL.stripH + CL.rowGap;
    }
    // The trailing rowGap doubles as the slab's bottom padding.
    return { rows, height: y };
})();

// Fixed block geometry. The DOM heights (uploadsH, promptH, gap) are pinned in
// refpack.css to the same values — the two must agree, there is no measurement.
// THE WIDTH PRESERVES THE NO-SCROLL INVARIANT: the longest row is 9 image tiles
// (9×131 + 8×6 = 1227px) plus the 24px add-gap and the 44px add square, which is
// drawn dimmed even at cap = 1295px of row viewport; +2×10 slab padding = 1315
// canvas; +2×10 block inset = 1335 node — fixed at 1340 for slack. That invariant
// is why no scroll machinery exists; change tile/gap/x0/pad/addBtn/addGap only
// together with this width.
const CONTENT = {
    width: 1340,
    pad: 10, // side inset of the DOM block within the node
    uploadsH: 30, // .mmrp-uploads fixed height
    promptH: 110, // .mmrp-direction fixed height — clearly smaller than a canvas row
    gap: 8, // .mmrp-block flex gap
};
CONTENT.height =
    CONTENT.uploadsH + CONTENT.gap + CANVAS_ROWS.height + CONTENT.gap + CONTENT.promptH;

// The ONE node size. Height floors against the 19 output sockets; widgetY is where
// litegraph placed the DOM block below the native widgets (fallback for the beat
// before the first layout assigns last_y).
function fixedSize(node) {
    const widgetY = (node._mmrpDomWidget && node._mmrpDomWidget.last_y) || 80;
    const outputsMin = ((node.outputs && node.outputs.length) || 1) * 20 + 40;
    return [CONTENT.width, Math.max(widgetY + CONTENT.height + CONTENT.pad, outputsMin)];
}

// ---------------------------------------------------------------------------
// The tag rule. Mirrors minimax_refpack/refs.py ReferenceSet.assign_tags() exactly:
//   1. images, slot order -> <Picture 1..n>
//   2. per video, slot order: soundtrack's <Audio j> is assigned BEFORE the video's own
//      <Video k>; standalone audio continues the same <Audio> counter afterwards
// `refs` is {images: [{file}], videos: [{file, use_soundtrack}], audios: [{file}]} — the
// working state keeps references grouped by kind, one array per kind, in socket order.
// ---------------------------------------------------------------------------
export function assignTags(refs) {
    const images = (refs && refs.images) || [];
    const videos = (refs && refs.videos) || [];
    const audios = (refs && refs.audios) || [];
    const tagged = { images: [], videos: [], audios: [] };

    images.forEach((ref, i) => tagged.images.push({ ref, tag: `<Picture ${i + 1}>` }));

    let audioN = 0;
    videos.forEach((ref, i) => {
        let audioTag = null;
        if (ref.use_soundtrack) {
            audioN += 1;
            audioTag = `<Audio ${audioN}>`;
        }
        tagged.videos.push({ ref, tag: `<Video ${i + 1}>`, audioTag });
    });

    audios.forEach((ref) => {
        audioN += 1;
        tagged.audios.push({ ref, tag: `<Audio ${audioN}>` });
    });

    return tagged;
}

// ---- references_json <-> working-state conversion --------------------
// refs.py's Reference shape:
//   {"kind": "image"|"video"|"audio", "file": str, [use_soundtrack], [crop], [trim]}
// crop = [x, y, w, h] fractions (image/video), trim = [start, end] seconds
// (video/audio) — both omitted when unset, mirroring refs.py's to_dict().

function takeEdit(v) {
    return Array.isArray(v) ? v.slice() : null;
}

export function fromReferencesList(list) {
    const refs = { images: [], videos: [], audios: [] };
    for (const r of list || []) {
        if (!r || typeof r.file !== "string") continue;
        if (r.kind === "image")
            refs.images.push({ file: r.file, missing: !!r.missing, crop: takeEdit(r.crop) });
        else if (r.kind === "video")
            refs.videos.push({
                file: r.file,
                use_soundtrack: !!r.use_soundtrack,
                missing: !!r.missing,
                crop: takeEdit(r.crop),
                trim: takeEdit(r.trim),
            });
        else if (r.kind === "audio")
            refs.audios.push({ file: r.file, missing: !!r.missing, trim: takeEdit(r.trim) });
    }
    return refs;
}

export function toReferencesList(refs) {
    const out = [];
    const withEdits = (d, r) => {
        if (Array.isArray(r.crop)) d.crop = r.crop.slice();
        if (Array.isArray(r.trim)) d.trim = r.trim.slice();
        return d;
    };
    for (const r of refs.images) out.push(withEdits({ kind: "image", file: r.file }, { crop: r.crop }));
    for (const r of refs.videos)
        out.push(withEdits({ kind: "video", file: r.file, use_soundtrack: !!r.use_soundtrack }, r));
    for (const r of refs.audios) out.push(withEdits({ kind: "audio", file: r.file }, { trim: r.trim }));
    return out;
}

// ---------------------------------------------------------------------------
// Crop/trim pure helpers — fraction-space math only, no DOM, so a node harness
// can exercise them the way pytest exercises refs.py.
// ---------------------------------------------------------------------------

// The badge's extra line: "2.00-6.50s · cropped" / "2.00-6.50s" / "cropped",
// or null when the reference is untouched.
export function editSummary(ref) {
    const parts = [];
    if (ref && Array.isArray(ref.trim)) parts.push(`${ref.trim[0].toFixed(2)}-${ref.trim[1].toFixed(2)}s`);
    if (ref && Array.isArray(ref.crop)) parts.push("cropped");
    return parts.length ? parts.join(" · ") : null;
}

function hasEdit(ref) {
    return !!(ref && (Array.isArray(ref.crop) || Array.isArray(ref.trim)));
}

function clamp01(v, lo, hi) {
    return Math.min(Math.max(v, lo), hi);
}

// What Save actually writes: round to 4 decimals (plenty below one pixel at 8K),
// clamp into the unit square with w capped at 1-x so refs.py's validate_crop can
// never reject a rect this produced, and collapse a (near-)full-frame rect to null
// — no crop at all, so an untouched reference serialises exactly as before.
export function normalizeCrop(rect) {
    if (!rect) return null;
    const r4 = (v) => Math.round(v * 1e4) / 1e4;
    const x = clamp01(r4(rect[0]), 0, 1);
    const y = clamp01(r4(rect[1]), 0, 1);
    const w = Math.min(r4(rect[2]), r4(1 - x));
    const h = Math.min(r4(rect[3]), r4(1 - y));
    if (w <= 0 || h <= 0) return null;
    if (x === 0 && y === 0 && w === 1 && h === 1) return null;
    return [x, y, w, h];
}

// What Save writes for the trim: [start, end] rounded to 2dp, or null when the
// window covers (within the 2dp rounding, 4ms) the whole clip — no trim at all,
// mirroring normalizeCrop's full-frame collapse. Also the predicate behind the
// modal's "Clear trim" disabled state: null here means there is nothing to clear.
export function normalizeTrim(trim, duration) {
    if (!trim || !duration) return null;
    const r2 = (v) => Math.round(v * 100) / 100;
    const s = r2(trim[0]);
    const e = r2(trim[1]);
    if (e <= s) return null;
    if (s <= 0.004 && e >= r2(duration) - 0.004) return null;
    return [s, e];
}

// One pointer-drag step over a fraction rect. mode = "move" | "nw"|"ne"|"sw"|"se";
// dx/dy are pointer deltas as fractions of the media box. `ratio` (a PIXEL w:h,
// e.g. 16/9) locks the rect's pixel aspect, which in fraction space means
// hFrac = wFrac * mediaW / (ratio * mediaH). The corner opposite the dragged one
// is the anchor and never moves.
export function dragCrop(rect, mode, dx, dy, ratio, mediaW, mediaH) {
    const MIN = 0.02;
    const [x, y, w, h] = rect;
    if (mode === "move") {
        return [clamp01(x + dx, 0, 1 - w), clamp01(y + dy, 0, 1 - h), w, h];
    }
    const ax = mode === "nw" || mode === "sw" ? x + w : x;
    const ay = mode === "nw" || mode === "ne" ? y + h : y;
    const cx = clamp01((mode === "nw" || mode === "sw" ? x : x + w) + dx, 0, 1);
    const cy = clamp01((mode === "nw" || mode === "ne" ? y : y + h) + dy, 0, 1);
    let nw = Math.max(Math.abs(cx - ax), MIN);
    let nh = Math.max(Math.abs(cy - ay), MIN);
    if (ratio && mediaW && mediaH) {
        nh = (nw * mediaW) / (ratio * mediaH);
        const maxH = cy >= ay ? 1 - ay : ay; // room on the side being dragged into
        if (nh > maxH) {
            nh = maxH;
            nw = (nh * ratio * mediaH) / mediaW;
        }
    }
    const nx = cx >= ax ? ax : ax - nw;
    const ny = cy >= ay ? ay : ay - nh;
    return [clamp01(nx, 0, 1 - nw), clamp01(ny, 0, 1 - nh), nw, nh];
}

// An aspect-preset click: reshape the current rect around its own centre to the
// given PIXEL ratio, spilling as little as possible past the frame.
export function setRectAspect(rect, ratio, mediaW, mediaH) {
    const [x, y, w, h] = rect;
    const cx = x + w / 2;
    const cy = y + h / 2;
    let nw = w;
    let nh = (w * mediaW) / (ratio * mediaH);
    if (nh > 1) {
        nw = nw / nh;
        nh = 1;
    }
    return [clamp01(cx - nw / 2, 0, 1 - nw), clamp01(cy - nh / 2, 0, 1 - nh), nw, nh];
}

/** Framing for "play the modified video": the crop region blown up to fill the same
 * footprint the untouched media occupies, so the preview shows what the socket emits
 * rather than a rectangle drawn over the original.
 *
 * `dw`/`dh` are the media's CURRENT displayed size in CSS px. The crop region is
 * scaled by min(dw/cw, dh/ch) - the largest scale that still fits the footprint - so
 * the region's own aspect is preserved and the box is never overflowed. The media
 * itself scales by the same factor and shifts by the crop origin, which is what puts
 * the region under the wrapper's visible window.
 */
export function cropPreviewBox(crop, dw, dh) {
    const [x, y, w, h] = crop;
    const cw = dw * w;
    const ch = dh * h;
    const scale = Math.min(dw / cw, dh / ch);
    return {
        wrapW: cw * scale,
        wrapH: ch * scale,
        mediaW: dw * scale,
        mediaH: dh * scale,
        left: -x * dw * scale,
        top: -y * dh * scale,
    };
}

function emptyRefs() {
    return { images: [], videos: [], audios: [] };
}

function cloneRefs(refs) {
    return {
        images: refs.images.map((r) => ({ ...r })),
        videos: refs.videos.map((r) => ({ ...r })),
        audios: refs.audios.map((r) => ({ ...r })),
    };
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiUpload(file) {
    const form = new FormData();
    form.append("image", file);
    form.append("type", "input");
    form.append("overwrite", "true");
    const res = await fetch("/upload/image", { method: "POST", body: form });
    if (!res.ok) throw new Error(`upload failed: ${res.status}`);
    const info = await res.json();
    return info.name;
}

// The tile thumb: cropped by the route (media.thumbnail_png), and for a trimmed
// video taken at the in-point (`t=`) so the tile previews the span that will
// actually be emitted, not frame 0 of the untrimmed file.
function thumbUrl(file, ref) {
    let url = `/minimax_refpack/thumb?file=${encodeURIComponent(file)}`;
    if (ref && Array.isArray(ref.crop)) url += `&crop=${ref.crop.join(",")}`;
    if (ref && Array.isArray(ref.trim)) url += `&t=${ref.trim[0]}`;
    return url;
}

// Raw file, for the click-to-play previews (thumbUrl is a server-generated still).
// Stock ComfyUI route — confirmed at server.py:511 (`@routes.get("/view")`, no /api prefix).
function fileUrl(file) {
    return `/view?filename=${encodeURIComponent(file)}&type=input`;
}

// NOTE: there is no apiSavePack/apiLoadPack/apiListPacks any more. Configs are files
// on the user's own machine (download + file picker, see "Config save/load" below),
// so the /minimax_refpack/packs routes are no longer called from the UI at all.

async function apiSystemPromptDefault() {
    const res = await fetch("/minimax_refpack/system_prompt");
    if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
    const data = await res.json();
    return data.default || "";
}

// Deliberately asks the SERVER to probe rather than probing from here. Two reasons, and
// the first is the one that matters: `localhost` has to mean whatever the ComfyUI process
// can reach, so a Dockerised or remote ComfyUI gets told the truth about its own network
// instead of about the machine this browser happens to be sitting on. The second is that
// a local LLM server has no reason to send CORS headers, so the browser could open the
// socket and still not be allowed to read the answer.
async function apiDetectServers(base) {
    const qs = base ? `?base=${encodeURIComponent(base)}` : "";
    const res = await fetch(`/minimax_refpack/detect${qs}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `detect failed: ${res.status}`);
    return data.servers || [];
}

// One probe per filename, cached module-wide and reused across every redraw (a
// naive per-draw fetch would hit this route constantly). probeResults holds the
// RESOLVED value so draw() — which is synchronous — can read it without awaiting:
// absent = still pending, true/false = probe's answer, null = probe itself failed
// (unknown, not "no audio").
const probeCache = new Map();
const probeResults = new Map();

function probeHasAudio(file) {
    if (!probeCache.has(file)) {
        probeCache.set(
            file,
            fetch(`/minimax_refpack/probe?file=${encodeURIComponent(file)}`)
                .then((res) => (res.ok ? res.json() : null))
                .then((data) => (data ? !!data.has_audio : null))
                .catch(() => null)
        );
    }
    return probeCache.get(file);
}

// Kick (or re-kick) probes for every current video and repaint when they answer.
// A saved use_soundtrack=true against a clip the probe says is SILENT (e.g. a config
// restored against a since-replaced file) is forced off — otherwise a stale <Audio N>
// tag points at nothing. Probe FAILURE (null) does not force off: "couldn't check"
// is not "no audio", so the user's saved intent survives a flaky probe.
function syncProbes(node) {
    for (const ref of node._mmrpRefs.videos) {
        const file = ref.file;
        probeHasAudio(file).then((hasAudio) => {
            probeResults.set(file, hasAudio);
            if (hasAudio === false) {
                const cur = node._mmrpRefs.videos.find((v) => v.file === file);
                if (cur && cur.use_soundtrack) {
                    const next = cloneRefs(node._mmrpRefs);
                    next.videos.find((v) => v.file === file).use_soundtrack = false;
                    applyRefs(node, next);
                    return; // applyRefs already repaints
                }
            }
            scheduleDraw(node);
        });
    }
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

function injectStyles() {
    if (document.getElementById("mmrp-styles")) return;
    const link = document.createElement("link");
    link.id = "mmrp-styles";
    link.rel = "stylesheet";
    link.href = new URL("./refpack.css", import.meta.url).href;
    document.head.appendChild(link);
}

// ---------------------------------------------------------------------------
// Hidden-widget helper — adapted from WhatDreamsCost multi_image_loader.js's
// hide-widget pattern: defineProperty-lock hidden/type against V3's reactive
// redraws, force computeSize unconditionally, poll for the async-created DOM
// element. See the header comment for why every half of this is load-bearing.
// ---------------------------------------------------------------------------

// Returns the hide-poll interval id (or undefined if there was no widget) so the
// caller can clear it early on node removal.
function hideWidget(w) {
    if (!w) return undefined;
    // Every write here is try/catch-wrapped: some frontend versions define these as
    // getter-only reactive accessors (that's exactly what broke on `domWidget.node =`,
    // see the header comment) — degrade instead of throwing and aborting workflow load.
    try {
        Object.defineProperty(w, "hidden", { get: () => true, set: () => {} });
    } catch (_) {
        try {
            w.hidden = true;
        } catch (_) {}
    }
    try {
        Object.defineProperty(w, "type", { get: () => "hidden", set: () => {} });
    } catch (_) {}
    try {
        if (!w.options) w.options = {};
        w.options.hidden = true;
    } catch (_) {}
    // computeSize = [0,0] UNCONDITIONALLY. Gating this behind a "skip on vueNodesMode"
    // check (a previous bug) left these multiline STRING widgets reserving real layout
    // height on V3 — stacked, that's the thin bar that overlapped the upload row.
    try {
        w.computeSize = () => [0, 0];
    } catch (_) {}
    if (!window.LiteGraph || !window.LiteGraph.vueNodesMode) {
        try {
            w.draw = () => {};
        } catch (_) {}
    }
    try {
        if (w.element) w.element.style.display = "none";
    } catch (_) {}
    // V3 creates the DOM element backing a multiline STRING widget ASYNCHRONOUSLY — it
    // does not exist yet at this point in onNodeCreated, so the display:none above is a
    // no-op the first time through. Poll briefly to catch it once it appears ("Catch
    // for V3 delayed DOM rendering to ensure no stubborn inputs appear" — same as the
    // reference). Self-clears after 1s; the onRemoved wrapper also clears it early.
    const hideInterval = setInterval(() => {
        try {
            if (w.element) w.element.style.display = "none";
        } catch (_) {}
    }, 50);
    setTimeout(() => clearInterval(hideInterval), 1000);
    return hideInterval;
}

// ---------------------------------------------------------------------------
// Thumbnail cache — module-wide, keyed by filename, shared across node instances.
// The thumb route serves a PNG for images AND videos (media.py thumbnail_png), so
// both kinds draw the same way; audio tiles get a drawn speaker glyph instead.
// liveNodes tracks which nodes to repaint when a thumb lands.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Structured logging. Same line shape as the Python side (minimax_refpack/logs.py):
// `[MiniMaxRefPack] event=<name> key=value`, so one grep covers the browser console
// and the ComfyUI console, and a bug report from either reads the same way.
//
// info = state changes worth seeing by default (uploads, edits, previews, config).
// debug = per-tile chatter (thumb retries), hidden until the console's Verbose level.
// warn = something the user will notice going wrong.
// ---------------------------------------------------------------------------

const LOG_PREFIX = "[MiniMaxRefPack]";

function logValue(value) {
    if (typeof value === "boolean") return value ? "true" : "false";
    if (typeof value === "number") {
        // 3dp, trailing zeros dropped - matches logs.py so the two consoles agree
        return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
    }
    if (Array.isArray(value)) return `[${value.map(logValue).join(",")}]`;
    const text = String(value);
    if (!text) return '""';
    return /[ "]/.test(text) ? `"${text.replace(/"/g, "'")}"` : text;
}

export function logLine(event, fields) {
    let line = `${LOG_PREFIX} event=${event}`;
    for (const [key, value] of Object.entries(fields || {})) {
        if (value === undefined || value === null) continue;
        line += ` ${key}=${logValue(value)}`;
    }
    return line;
}

const mlog = (event, fields) => console.info(logLine(event, fields));
const mdebug = (event, fields) => console.debug(logLine(event, fields));
const mwarn = (event, fields) => console.warn(logLine(event, fields));

const thumbCache = new Map(); // file -> { img, state: "loading"|"ok"|"error", tries, reqs, timer }
const liveNodes = new Set();

// A thumb request fails for reasons that have nothing to do with the file: thumb_route
// decodes SYNCHRONOUSLY on the aiohttp loop, so while ComfyUI is still starting up (model
// loads, Manager's registry fetch) the request stalls and whatever proxy sits in front of
// the pod kills it. The first version cached that <img> error forever — one unlucky
// request and the tile read "no preview" until the tab was reloaded, with the file sitting
// happily in input/ the whole time. So: back off and retry, and only claim "no preview"
// once the ladder is exhausted.
const THUMB_RETRY_MS = [1000, 2000, 4000, 8000, 15000];

function repaintLive() {
    for (const n of liveNodes) scheduleDraw(n);
}

// `reqs` is monotonic and only feeds the cache-buster — a retry must not be answered from
// whatever cached the failure. The first request stays clean so it can be cached normally.
// `entry.base` carries the crop/trim query the entry was created with; an edit drops the
// whole entry (dropThumb) rather than mutating it.
function requestThumb(entry) {
    entry.timer = null;
    entry.reqs += 1;
    entry.img.src = entry.reqs === 1 ? entry.base : `${entry.base}&retry=${entry.reqs - 1}`;
}

function getThumb(file, ref) {
    let entry = thumbCache.get(file);
    if (!entry) {
        const img = new Image();
        entry = { img, file, base: thumbUrl(file, ref), state: "loading", tries: 0, reqs: 0, timer: null };
        img.onload = () => {
            entry.state = "ok";
            entry.tries = 0;
            repaintLive();
        };
        img.onerror = () => {
            const delay = THUMB_RETRY_MS[entry.tries];
            entry.tries += 1;
            if (delay === undefined) {
                // Ladder spent (~30s). Say so on the tile — but the entry stays retryable,
                // see retryFailedThumbs().
                entry.state = "error";
                mwarn("thumb_failed", { file: entry.file, tries: entry.tries - 1 });
                repaintLive();
                return;
            }
            // Still trying: the tile shows the plain well, not a verdict it may have to
            // take back a second later.
            entry.state = "loading";
            mdebug("thumb_retry", { file: entry.file, attempt: entry.tries, in_ms: delay });
            entry.timer = setTimeout(() => requestThumb(entry), delay);
        };
        thumbCache.set(file, entry);
        requestThumb(entry);
    }
    return entry;
}

// Saving an edit invalidates the file's cached thumb so the next draw re-fetches
// through the route with the new crop/t baked into the URL.
function dropThumb(file) {
    const entry = thumbCache.get(file);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    entry.img.onload = null;
    entry.img.onerror = null;
    thumbCache.delete(file);
}

// Giving up is not giving up for good. Anything that says the world may have changed — the
// tab coming back to the front, the browser going back online — puts every failed thumb
// back in flight, so a pod that took minutes to finish booting recovers on its own instead
// of demanding a page reload. Cheap: entries that loaded fine are skipped, and nothing
// here polls.
function retryFailedThumbs() {
    let any = false;
    for (const [file, entry] of thumbCache) {
        if (entry.state !== "error") continue;
        entry.state = "loading";
        entry.tries = 0;
        requestThumb(entry);
        any = true;
    }
    if (any) repaintLive();
}

window.addEventListener("focus", retryFailedThumbs);
window.addEventListener("online", retryFailedThumbs);

// The single repaint funnel: every change (refs, selection, thumb load, probe
// answer, preview toggle, the canvas's one real mount) lands here, coalesced to one
// draw() per frame. NOT a permanent rAF loop — nothing re-queues unless something
// changes again.
function scheduleDraw(node) {
    if (node._mmrpDrawQueued) return;
    node._mmrpDrawQueued = true;
    requestAnimationFrame(() => {
        node._mmrpDrawQueued = false;
        draw(node);
    });
}

// ---------------------------------------------------------------------------
// Drawing. All geometry in CSS px; hit regions are recorded during draw() in the
// same space getMousePos() reports, so hit-testing is a point-in-rect scan over
// what was actually painted last.
// ---------------------------------------------------------------------------

function pathRoundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
}

// object-fit: cover, in canvas terms — crop the source centrally to the tile's
// aspect instead of squashing it.
function drawCover(ctx, img, x, y, w, h) {
    const iw = img.naturalWidth;
    const ih = img.naturalHeight;
    if (!iw || !ih) return;
    const s = Math.max(w / iw, h / ih);
    const sw = w / s;
    const sh = h / s;
    ctx.drawImage(img, (iw - sw) / 2, (ih - sh) / 2, sw, sh, x, y, w, h);
}

// Monochrome vector speaker for audio tiles — a fillText emoji would render in
// color and break the greyscale palette.
function drawSpeaker(ctx, cx, cy, s, color) {
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(cx - s * 0.7, cy - s * 0.28);
    ctx.lineTo(cx - s * 0.3, cy - s * 0.28);
    ctx.lineTo(cx + s * 0.1, cy - s * 0.65);
    ctx.lineTo(cx + s * 0.1, cy + s * 0.65);
    ctx.lineTo(cx - s * 0.3, cy + s * 0.28);
    ctx.lineTo(cx - s * 0.7, cy + s * 0.28);
    ctx.closePath();
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx + s * 0.25, cy, s * 0.45, -Math.PI / 3, Math.PI / 3);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx + s * 0.25, cy, s * 0.78, -Math.PI / 3, Math.PI / 3);
    ctx.stroke();
    ctx.restore();
}

// The click-to-play affordance: dark circle, drawn ▶ or ❚❚ (vector, no emoji).
function drawPlayGlyph(ctx, cx, cy, playing) {
    ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
    ctx.beginPath();
    ctx.arc(cx, cy, CL.playR, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = C.text;
    if (playing) {
        ctx.fillRect(cx - 6, cy - 6.5, 4.5, 13);
        ctx.fillRect(cx + 1.5, cy - 6.5, 4.5, 13);
    } else {
        ctx.beginPath();
        ctx.moveTo(cx - 4.5, cy - 7);
        ctx.lineTo(cx + 8, cy);
        ctx.lineTo(cx - 4.5, cy + 7);
        ctx.closePath();
        ctx.fill();
    }
}

// The crop/trim entry point: a scissors chip in the tile's top-left — the same quiet
// dark-circle family as the delete chip (vector, no emoji). Inverts to a light chip
// while the reference carries an edit, mirroring how ♪ signals on/off.
function drawEditChip(ctx, x, y, edited) {
    const dR = CL.del / 2;
    const cx = x + dR + 3;
    const cy = y + dR + 3;
    ctx.fillStyle = edited ? C.text : "rgba(0, 0, 0, 0.65)";
    ctx.beginPath();
    ctx.arc(cx, cy, dR, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = edited ? "#000" : "#555";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(cx, cy, dR - 0.5, 0, Math.PI * 2);
    ctx.stroke();
    // Scissors: two crossing blades up, two finger loops down.
    const g = edited ? "#000" : "#fff";
    ctx.strokeStyle = g;
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.moveTo(cx - 2.2, cy + 1.6);
    ctx.lineTo(cx + 4, cy - 4.2);
    ctx.moveTo(cx + 2.2, cy + 1.6);
    ctx.lineTo(cx - 4, cy - 4.2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx - 3.1, cy + 3.1, 1.7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx + 3.1, cy + 3.1, 1.7, 0, Math.PI * 2);
    ctx.stroke();
}

// One tile: well, thumbnail/glyph, tag badge, delete, edit chip, soundtrack state,
// play glyph, missing treatment, selection stroke. `lines` is the pre-computed badge
// text (up to three: tag, a video's <Audio N>, the crop/trim summary); `playState` is
// null | "play" | "pause" (null = no preview affordance: images, missing refs).
function drawTile(ctx, kind, ref, lines, x, y, selected, soundState, badgeH, playState) {
    const T = CL.tile;

    ctx.save();
    pathRoundRect(ctx, x, y, T, T, 4);
    ctx.clip();

    ctx.fillStyle = kind === "audio" ? C.surface : C.well;
    ctx.fillRect(x, y, T, T);

    if (kind === "audio") {
        // Speaker in the upper half; the play glyph takes the lower half.
        drawSpeaker(ctx, x + T / 2, y + 34, 18, ref.missing ? C.textDim : C.textFaint);
    } else {
        const entry = getThumb(ref.file, ref);
        if (entry.state === "ok") {
            if (ref.missing) ctx.globalAlpha = 0.6;
            drawCover(ctx, entry.img, x, y, T, T);
            ctx.globalAlpha = 1;
        } else if (entry.state === "error" && !ref.missing) {
            ctx.fillStyle = C.textDim;
            ctx.font = "11px sans-serif";
            ctx.textAlign = "center";
            ctx.fillText("no preview", x + T / 2, y + T / 2);
        }
    }

    if (ref.missing) {
        ctx.fillStyle = C.danger;
        ctx.font = "bold 11px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("missing", x + T / 2, y + T - badgeH - 8);
    }

    if (playState) {
        // Video: slightly above center to clear the badge; audio: lower half.
        drawPlayGlyph(ctx, x + T / 2, kind === "audio" ? y + 80 : y + 58, playState === "pause");
    }

    // Tag badge — drawn text on a translucent strip pinned to the tile's bottom.
    // Deliberately dark: it always overlays a thumbnail or a lightened well, never
    // the black ground, and #e0e0e0 text carries it.
    ctx.fillStyle = "rgba(0, 0, 0, 0.7)";
    ctx.fillRect(x, y + T - badgeH, T, badgeH);
    ctx.fillStyle = C.text;
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";
    lines.forEach((line, i) => {
        ctx.fillText(line, x + T / 2, y + T - badgeH + 13 + i * 13);
    });

    // Delete affordance — always visible (canvas has no cheap hover), but QUIET:
    // a dark chip, not a red square. Red is the node's one accent and it belongs
    // to selection, not to a destructive secondary action (UI review #5).
    const dR = CL.del / 2;
    const dcx = x + T - dR - 3;
    const dcy = y + dR + 3;
    ctx.fillStyle = "rgba(0, 0, 0, 0.65)";
    ctx.beginPath();
    ctx.arc(dcx, dcy, dR, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#555";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(dcx, dcy, dR - 0.5, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(dcx - 4, dcy - 4);
    ctx.lineTo(dcx + 4, dcy + 4);
    ctx.moveTo(dcx + 4, dcy - 4);
    ctx.lineTo(dcx - 4, dcy + 4);
    ctx.stroke();

    // Crop/trim entry point — top-left, the one corner nothing else owns. A missing
    // file has no media to show in the editor, so it gets no chip.
    if (!ref.missing) drawEditChip(ctx, x, y, hasEdit(ref));

    if (soundState) {
        const S = CL.sound;
        const sy = y + T - badgeH - S - 3;
        const on = soundState === "on";
        // FULLY OPAQUE chip (LEDGER "see-through music icon"): the old fills were
        // rgba(255,255,255,0.15) when on / rgba(0,0,0,0.65) otherwise, compositing
        // the thumbnail through — on a light or busy clip the toggle all but
        // vanished. Solid fills only, with a border to lift the chip's edge off a
        // dark thumbnail; "on" inverts to a light chip with a black glyph so the
        // two states can't be confused. Same rect, same radius — geometry untouched.
        ctx.fillStyle = on ? C.text : "#111";
        pathRoundRect(ctx, x + 3, sy, S, S, 4);
        ctx.fill();
        ctx.strokeStyle = on ? "#000" : "#555";
        ctx.lineWidth = 1;
        pathRoundRect(ctx, x + 3.5, sy + 0.5, S - 1, S - 1, 4);
        ctx.stroke();
        ctx.fillStyle =
            soundState === "pending" ? "#555" : soundState === "none" ? "#444" : on ? "#000" : C.textMuted;
        ctx.font = "16px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("♪", x + 3 + S / 2, sy + 20);
        if (soundState === "none") {
            // Struck through: this clip has no audio track, permanently disabled.
            ctx.strokeStyle = "#555";
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x + 7, sy + S - 5);
            ctx.lineTo(x + S - 1, sy + 5);
            ctx.stroke();
        }
    }

    ctx.restore();

    ctx.strokeStyle = ref.missing ? C.danger : C.border;
    ctx.lineWidth = 1;
    pathRoundRect(ctx, x + 0.5, y + 0.5, T - 1, T - 1, 4);
    ctx.stroke();

    if (selected) {
        // The one accent in the whole node: LTX-red rectangle around the tile.
        ctx.strokeStyle = C.danger;
        ctx.lineWidth = 3;
        pathRoundRect(ctx, x - 2.5, y - 2.5, T + 5, T + 5, 7);
        ctx.stroke();
    }
}

// Vector upload arrow — same family as the toolbar's SVG icon (shaft, chevron
// head, tray) so the two read as one set. Drawn, not text; no emoji.
function drawUploadGlyph(ctx, cx, cy, s, color) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(cx, cy + s * 0.15);
    ctx.lineTo(cx, cy - s * 0.5);
    ctx.moveTo(cx - s * 0.3, cy - s * 0.2);
    ctx.lineTo(cx, cy - s * 0.5);
    ctx.lineTo(cx + s * 0.3, cy - s * 0.2);
    ctx.moveTo(cx - s * 0.45, cy + s * 0.3);
    ctx.lineTo(cx - s * 0.45, cy + s * 0.5);
    ctx.lineTo(cx + s * 0.45, cy + s * 0.5);
    ctx.lineTo(cx + s * 0.45, cy + s * 0.3);
    ctx.stroke();
    ctx.restore();
}

// The per-row add affordance (UI review #1): an icon-only square that opens that
// row's file picker. Deliberately NOT the toolbar-button treatment — a toolbar
// clone sitting on the media surface read wrong; this is a quiet well with an
// upload glyph. Drawn dimmed (not removed) at cap: a control that vanishes
// teaches nothing, a dimmed one shows the row is full (UI review #4).
function drawAddSquare(ctx, x, y, dimmed) {
    const S = CL.addBtn;
    ctx.save();
    if (dimmed) ctx.globalAlpha = 0.4;
    ctx.fillStyle = C.wellDeep;
    pathRoundRect(ctx, x + 0.5, y + 0.5, S - 1, S - 1, 6);
    ctx.fill();
    ctx.strokeStyle = C.raised;
    ctx.lineWidth = 1;
    pathRoundRect(ctx, x + 0.5, y + 0.5, S - 1, S - 1, 6);
    ctx.stroke();
    drawUploadGlyph(ctx, x + S / 2, y + S / 2, 18, C.textFaint);
    ctx.restore();
}

function draw(node) {
    const body = node._mmrpBody;
    if (!body) return;
    const canvas = body.canvas;
    const ctx = body.ctx;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    // Not attached / zero-sized yet (V3 mounts the DOM widget asynchronously) —
    // the ResizeObserver fires once real dimensions arrive and repaints then.
    if (!cssW || !cssH) return;

    // HiDPI: back the canvas at devicePixelRatio and scale the context so all
    // layout math (and hit regions) stay in CSS px while text/thumbs stay sharp.
    const dpr = window.devicePixelRatio || 1;
    const bw = Math.round(cssW * dpr);
    const bh = Math.round(cssH * dpr);
    if (canvas.width !== bw || canvas.height !== bh) {
        canvas.width = bw;
        canvas.height = bh;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    // The slab itself: the CSS keeps the element black; a subtle drawn edge marks
    // it off against the grey node body without the border-vs-content-box coordinate
    // mismatch a CSS border would introduce into hit-testing.
    ctx.strokeStyle = C.raised;
    ctx.lineWidth = 1;
    pathRoundRect(ctx, 0.5, 0.5, cssW - 1, cssH - 1, 6);
    ctx.stroke();

    const refs = node._mmrpRefs;
    const tagged = assignTags(refs);
    const viewW = cssW - CL.x0 * 2;
    const regions = [];
    node._mmrpHit = { regions };
    const playing = node._mmrpPlaying;

    for (const row of CANVAS_ROWS.rows) {
        const kind = row.kind;
        const arr = refs[`${kind}s`];
        const taggedArr = tagged[`${kind}s`];
        const atCap = arr.length >= CAPS[kind];
        const tileY = row.stripY + CL.stripPad;

        // Divider at the midpoint of the inter-section gap — per spec: a viewer
        // should never be unsure which section a tile belongs to.
        if (row.y > CL.padTop) {
            const dy = Math.round(row.y - CL.rowGap / 2) + 0.5;
            ctx.strokeStyle = "#2a2a2a";
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(CL.x0, dy);
            ctx.lineTo(CL.x0 + viewW, dy);
            ctx.stroke();
        }

        // The labels are the only wayfinding on the slab — #aaa/12px, not the
        // quietest text in the node (UI review #7). At cap they brighten further.
        ctx.fillStyle = atCap ? C.text : C.textMuted;
        ctx.font = "12px sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(`${SECTION_LABEL[kind]} (${arr.length}/${CAPS[kind]})`, CL.x0, row.y + 12);

        arr.forEach((ref, i) => {
            const tx = CL.x0 + i * (CL.tile + CL.gap);
            const t = taggedArr[i];
            // "vid_audio <Audio 1>" not bare "<Audio 1>": the tag is what MiniMax binds
            // and has to stay visible, but unlabelled it reads as a standalone audio ref.
            const lines = kind === "video" && t.audioTag
                ? [t.tag, `vid_audio ${t.audioTag}`]
                : [t.tag];
            // An edited reference states its edit on the badge: "2.00-6.50s · cropped".
            const summary = editSummary(ref);
            if (summary) lines.push(summary);
            const badgeH = [CL.badge1, CL.badge2, CL.badge3][lines.length - 1];
            const selected =
                node._mmrpSelected && node._mmrpSelected.kind === kind && node._mmrpSelected.index === i;

            let soundState = null;
            if (kind === "video") {
                const res = probeResults.has(ref.file) ? probeResults.get(ref.file) : undefined;
                soundState =
                    res === undefined
                        ? "pending"
                        : res === true
                          ? ref.use_soundtrack
                              ? "on"
                              : "off"
                          : res === false
                            ? "none"
                            : "unknown";
            }

            let playState = null;
            if (kind !== "image" && !ref.missing) {
                playState =
                    playing && playing.kind === kind && playing.file === ref.file ? "pause" : "play";
            }

            drawTile(ctx, kind, ref, lines, tx, tileY, selected, soundState, badgeH, playState);

            // Hit regions, most specific last — hitTest scans in reverse so the
            // play/delete/soundtrack/edit affordances win over the tile containing them.
            regions.push({ type: "tile", kind, index: i, file: ref.file, x: tx, y: tileY, w: CL.tile, h: CL.tile });
            if (!ref.missing) {
                regions.push({
                    type: "edit",
                    kind,
                    index: i,
                    file: ref.file,
                    x: tx + 3,
                    y: tileY + 3,
                    w: CL.del,
                    h: CL.del,
                });
            }
            if (soundState === "on" || soundState === "off") {
                regions.push({
                    type: "sound",
                    kind,
                    index: i,
                    file: ref.file,
                    x: tx + 3,
                    y: tileY + CL.tile - badgeH - CL.sound - 3,
                    w: CL.sound,
                    h: CL.sound,
                });
            }
            regions.push({
                type: "del",
                kind,
                index: i,
                file: ref.file,
                x: tx + CL.tile - CL.del - 3,
                y: tileY + 3,
                w: CL.del,
                h: CL.del,
            });
            if (playState) {
                const cy = kind === "audio" ? tileY + 80 : tileY + 58;
                regions.push({
                    type: "play",
                    kind,
                    index: i,
                    file: ref.file,
                    x: tx + CL.tile / 2 - CL.playR,
                    y: cy - CL.playR,
                    w: CL.playR * 2,
                    h: CL.playR * 2,
                    tileX: tx,
                    tileY,
                });
            }
        });

        // FIXED placement (UI review #2): always where the next tile would go —
        // the row's left edge when empty, after the last tile (plus the 24px
        // separating gap) otherwise. It never centres itself, so it never jumps;
        // it only ever moves rightwards as tiles are added. Vertically centred
        // on the strip. Drawn dimmed at cap, and only clickable below cap.
        const ax = arr.length
            ? CL.x0 + arr.length * CL.tile + (arr.length - 1) * CL.gap + CL.addGap
            : CL.x0;
        const ay = tileY + Math.round((CL.tile - CL.addBtn) / 2);
        drawAddSquare(ctx, ax, ay, atCap);
        if (!atCap) {
            regions.push({ type: "add", kind, x: ax, y: ay, w: CL.addBtn, h: CL.addBtn });
        }
    }
}

// ---------------------------------------------------------------------------
// Canvas hit-testing. ComfyUI zooms the whole DOM widget with a CSS transform,
// so a client-space pixel is NOT a canvas-space pixel — map through the bounding
// rect scaled back by the element's own layout size (ltx_director.js getMousePos,
// :3900). Everything comes out in the same CSS-px space draw() painted in.
// ---------------------------------------------------------------------------

function getMousePos(canvas, e) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return { x: -1, y: -1 };
    return {
        x: ((e.clientX - rect.left) * canvas.offsetWidth) / rect.width,
        y: ((e.clientY - rect.top) * canvas.offsetHeight) / rect.height,
    };
}

export function hitTest(node, x, y) {
    const hit = node._mmrpHit;
    if (!hit) return null;
    for (let i = hit.regions.length - 1; i >= 0; i--) {
        const r = hit.regions[i];
        if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return r;
    }
    return null;
}

function onCanvasMouseDown(node, e) {
    if (e.button !== 0) return;
    const pos = getMousePos(node._mmrpBody.canvas, e);
    const hit = hitTest(node, pos.x, pos.y);
    if (!hit) {
        if (node._mmrpSelected) {
            node._mmrpSelected = null;
            scheduleDraw(node);
        }
        return;
    }
    if (hit.type === "del") {
        removeRef(node, hit.kind, hit.index);
    } else if (hit.type === "sound") {
        toggleSoundtrack(node, hit.file);
    } else if (hit.type === "play") {
        togglePreview(node, hit);
    } else if (hit.type === "edit") {
        openEditModal(node, hit.kind, hit.index);
    } else if (hit.type === "add") {
        node._mmrpBody.fileInputs[hit.kind].click();
    } else {
        node._mmrpSelected = { kind: hit.kind, index: hit.index };
        scheduleDraw(node);
    }
}

// Double-clicking a tile opens the crop/trim editor too — same modal as the chip.
// The affordance chips keep their own single-click meanings.
function onCanvasDblClick(node, e) {
    const pos = getMousePos(node._mmrpBody.canvas, e);
    const hit = hitTest(node, pos.x, pos.y);
    if (!hit || (hit.type !== "tile" && hit.type !== "edit")) return;
    const ref = node._mmrpRefs[`${hit.kind}s`][hit.index];
    if (!ref || ref.missing) return;
    openEditModal(node, hit.kind, hit.index);
}

function toggleSoundtrack(node, file) {
    if (probeResults.get(file) !== true) return; // pending/silent/unknown — not toggleable
    const next = cloneRefs(node._mmrpRefs);
    const target = next.videos.find((v) => v.file === file);
    if (target) target.use_soundtrack = !target.use_soundtrack;
    if (target) mlog("soundtrack", { file, on: target.use_soundtrack });
    applyRefs(node, next);
}

// ---------------------------------------------------------------------------
// Click-to-play previews — ONE shared <video> overlay + ONE shared <audio> per
// node, never per-cell DOM. The rule: the drawn ▶ circle is the play hit region;
// clicking anywhere else on the tile selects. Any refs change stops the preview
// (tile positions shift, the overlay would sit on the wrong tile), as do 'ended',
// a second click, clicking the playing overlay itself, and onRemoved.
// ---------------------------------------------------------------------------

function stopPreview(node) {
    const body = node._mmrpBody;
    if (!body || !node._mmrpPlaying) return;
    body.previewVideo.pause();
    body.previewVideo.removeAttribute("src");
    body.previewVideo.style.display = "none";
    body.previewAudio.pause();
    body.previewAudio.removeAttribute("src");
    node._mmrpPlaying = null;
    scheduleDraw(node);
}

function togglePreview(node, hit) {
    const body = node._mmrpBody;
    const playing = node._mmrpPlaying;
    if (playing && playing.kind === hit.kind && playing.file === hit.file) {
        stopPreview(node);
        return;
    }
    stopPreview(node);
    // A trimmed reference previews ONLY its span: seek to the in-point here, stop at
    // the out-point in the shared elements' timeupdate handlers (buildCustomBlock).
    // Setting currentTime before metadata sets the default playback start position
    // (per the HTML spec), and the timeupdate handler re-seeks if a browser drops it.
    const ref = node._mmrpRefs[`${hit.kind}s`][hit.index];
    const trim = ref && Array.isArray(ref.trim) ? ref.trim : null;
    if (hit.kind === "video") {
        const v = body.previewVideo;
        v.src = fileUrl(hit.file);
        if (trim) v.currentTime = trim[0];
        // Same coordinate space as the canvas — the block is the offsetParent for
        // both, and ComfyUI's zoom transform applies to the overlay automatically.
        v.style.left = `${body.canvas.offsetLeft + hit.tileX}px`;
        v.style.top = `${body.canvas.offsetTop + hit.tileY}px`;
        v.style.display = "block";
        v.play().catch(() => stopPreview(node));
    } else {
        const a = body.previewAudio;
        a.src = fileUrl(hit.file);
        if (trim) a.currentTime = trim[0];
        a.play().catch(() => stopPreview(node));
    }
    node._mmrpPlaying = { kind: hit.kind, file: hit.file, trim };
    scheduleDraw(node);
}

// ---------------------------------------------------------------------------
// State <-> widgets
// ---------------------------------------------------------------------------

function widgetByName(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// Never let a malformed references_json abort workflow loading.
//
// ComfyUI restores widget values POSITIONALLY from widgets_values. Any change to the
// widget list — ours moved the DOM widget from first to last, and the Python side later
// added `system_prompt` — shifts that mapping for workflows saved by an older build, so
// this widget can come back holding a neighbour's value (a model id, an API key, a
// prompt). Parsing that threw a SyntaxError out of onNodeCreated/onConfigure and killed
// the entire workflow load, taking every other node with it.
//
// A reference set we cannot read is recoverable — the user re-adds the files. A workflow
// that will not open is not. So: parse defensively, fall back to empty, warn once.
function parseRefsValue(widget) {
    if (!widget) return emptyRefs();
    const raw = widget.value;
    if (typeof raw !== "string" || !raw.trim()) return emptyRefs();
    try {
        const parsed = JSON.parse(raw);
        return fromReferencesList((parsed && parsed.references) || []);
    } catch (e) {
        console.warn(
            "[MiniMaxRefPack] references_json held a value this build cannot read " +
                "(likely a widget-order shift from an older saved workflow). Starting with " +
                "no references; re-add them and re-save. Raw value:",
            raw
        );
        return emptyRefs();
    }
}

function syncReferencesWidget(node) {
    const w = widgetByName(node, "references_json");
    if (!w) return;
    const flat = [...node._mmrpRefs.images, ...node._mmrpRefs.videos, ...node._mmrpRefs.audios];
    const list = toReferencesList(node._mmrpRefs).map((r, i) => {
        // carry the "missing" flag through without teaching refs.py about it: it's
        // stripped by fromReferencesList's kind switch on the Python side.
        const src = flat[i];
        return src && src.missing ? { ...r, missing: true } : r;
    });
    w.value = JSON.stringify({ references: list });
}

function applyRefs(node, refs) {
    stopPreview(node); // tile positions shift with any change — never leave the overlay stale
    node._mmrpRefs = refs;
    syncReferencesWidget(node);
    renderNodeBody(node);
}

async function addFiles(node, kind, files) {
    const refs = cloneRefs(node._mmrpRefs);
    const arr = refs[`${kind}s`];
    const room = CAPS[kind] - arr.length;
    if (room <= 0) return;
    const all = Array.from(files);
    const take = all.slice(0, room);
    // Caps are hard, but never SILENTLY hard — say what didn't fit.
    if (take.length < all.length) {
        alert(`Only ${room} ${kind} slot${room === 1 ? "" : "s"} left — skipped ${all.length - take.length} file(s).`);
    }
    mlog("upload", { kind, files: take.length });
    const uploadStarted = performance.now();
    for (const file of take) {
        try {
            const name = await apiUpload(file);
            mlog("uploaded", { kind, file: name, bytes: file.size });
            // soundtrack ON by default; the probe turns it back off for a silent clip
            arr.push(kind === "video" ? { file: name, use_soundtrack: true } : { file: name });
        } catch (e) {
            mwarn("upload_failed", { kind, file: file.name, error: e.message });
            alert(`Upload failed for ${file.name}: ${e.message}`);
        }
    }
    mlog("upload_done", { kind, files: take.length, ms: performance.now() - uploadStarted });
    applyRefs(node, refs);
}

function removeRef(node, kind, index) {
    const refs = cloneRefs(node._mmrpRefs);
    const [gone] = refs[`${kind}s`].splice(index, 1);
    mlog("reference_removed", { kind, file: gone && gone.file });
    if (node._mmrpSelected && node._mmrpSelected.kind === kind) {
        if (node._mmrpSelected.index === index) node._mmrpSelected = null;
        else if (node._mmrpSelected.index > index) node._mmrpSelected.index -= 1;
    }
    applyRefs(node, refs);
}

// The DOM-side reaction to a state change: button disabled states, probes, repaint.
// No sizing here — the geometry is fixed, only the canvas's CONTENTS change.
function renderNodeBody(node) {
    const body = node._mmrpBody;
    if (!body) return;
    const refs = node._mmrpRefs;
    for (const kind of KINDS) {
        body.uploadButtons[kind].disabled = refs[`${kind}s`].length >= CAPS[kind];
    }
    syncProbes(node);
    scheduleDraw(node);
}

// ---------------------------------------------------------------------------
// Custom block: upload row (DOM buttons), the canvas, prompt textarea
// ---------------------------------------------------------------------------

// Monochrome upload-arrow icon (stroke: currentColor, so it inherits the button's
// white) — one family across all three upload buttons, per the sketch:
// [⬆ Image] [⬆ Video] [⬆ Audio]. The icon carries "upload", the label carries the
// kind, which is what lets the buttons shrink horizontally.
const UPLOAD_ICON =
    '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M8 10.5V3M4.5 6 8 2.5 11.5 6M2.5 12.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1"/></svg>';

const KIND_UPLOAD_META = [
    ["image", "Image", "image/*"],
    ["video", "Video", "video/*"],
    ["audio", "Audio", "audio/*"],
];

function buildCustomBlock(node) {
    const container = document.createElement("div");
    container.className = "mmrp-block";

    const uploadRow = document.createElement("div");
    uploadRow.className = "mmrp-uploads";
    const uploadButtons = {};
    const fileInputs = {};
    for (const [kind, label, accept] of KIND_UPLOAD_META) {
        const btn = document.createElement("button");
        btn.className = "mmrp-btn mmrp-upload-btn";
        btn.innerHTML = `${UPLOAD_ICON}<span>${label}</span>`;
        const input = document.createElement("input");
        input.type = "file";
        input.accept = accept;
        input.multiple = true;
        input.style.display = "none";
        input.onchange = async (e) => {
            await addFiles(node, kind, e.target.files);
            input.value = "";
        };
        btn.onclick = () => input.click();
        uploadRow.appendChild(btn);
        uploadRow.appendChild(input);
        uploadButtons[kind] = btn;
        fileInputs[kind] = input; // the per-row "+" add slots click these too
    }

    const saveConfigBtn = document.createElement("button");
    saveConfigBtn.className = "mmrp-btn mmrp-upload-btn";
    saveConfigBtn.textContent = "Save config";
    saveConfigBtn.onclick = () => saveConfig(node, saveConfigBtn);
    uploadRow.appendChild(saveConfigBtn);

    const loadConfigBtn = document.createElement("button");
    loadConfigBtn.className = "mmrp-btn mmrp-upload-btn";
    loadConfigBtn.textContent = "Load config";
    loadConfigBtn.onclick = () => openLoadConfigPanel(node, loadConfigBtn);
    uploadRow.appendChild(loadConfigBtn);

    const localBtn = document.createElement("button");
    localBtn.className = "mmrp-btn mmrp-upload-btn mmrp-local-btn";
    localBtn.textContent = "Local LLM";
    localBtn.title = "Find an OpenAI-compatible server on this machine and use it to write prompts";
    localBtn.onclick = () => openLocalServerModal(node, localBtn);
    uploadRow.appendChild(localBtn);
    // Only meaningful while prompt_provider is `local`, so it is hidden otherwise rather
    // than sitting there inert. The row is nowrap and content-sized, so removing it from
    // layout just shortens the group; the node's fixed height is unaffected.
    node._mmrpLocalBtn = localBtn;
    syncLocalBtn(node);

    const gearBtn = document.createElement("button");
    gearBtn.className = "mmrp-btn mmrp-gear-btn";
    gearBtn.textContent = "⚙";
    gearBtn.title = "System prompt settings";
    gearBtn.onclick = () => openSystemPromptModal(node);
    uploadRow.appendChild(gearBtn);

    container.appendChild(uploadRow);

    const canvas = document.createElement("canvas");
    canvas.className = "mmrp-canvas";
    canvas.style.height = `${CANVAS_ROWS.height}px`; // fixed, set once
    canvas.addEventListener("mousedown", (e) => onCanvasMouseDown(node, e));
    canvas.addEventListener("dblclick", (e) => onCanvasDblClick(node, e));
    // Keep litegraph's node context menu off the tiles (matches the reference's
    // per-item contextmenu swallow).
    canvas.addEventListener("contextmenu", (e) => e.stopPropagation());
    container.appendChild(canvas);

    // Fires exactly once in practice — when V3 asynchronously mounts the block and
    // the canvas goes 0 -> real size. That first fire is the initial paint trigger;
    // the geometry never changes afterwards.
    const resizeObserver = new ResizeObserver(() => scheduleDraw(node));
    resizeObserver.observe(canvas);

    // The shared preview pair (see togglePreview). Children of the block so the
    // canvas and the overlay share an offsetParent and a zoom transform.
    const previewVideo = document.createElement("video");
    previewVideo.className = "mmrp-preview";
    previewVideo.playsInline = true;
    previewVideo.addEventListener("ended", () => stopPreview(node));
    previewVideo.addEventListener("click", () => stopPreview(node));
    container.appendChild(previewVideo);

    const previewAudio = document.createElement("audio");
    previewAudio.addEventListener("ended", () => stopPreview(node));
    container.appendChild(previewAudio);

    // Trim enforcement for the shared preview pair: stop at the out-point, and
    // re-seek to the in-point if the pre-metadata seek in togglePreview was dropped.
    const keepToTrim = (el) => () => {
        const p = node._mmrpPlaying;
        if (!p || !p.trim) return;
        if (el.currentTime >= p.trim[1]) stopPreview(node);
        else if (el.currentTime < p.trim[0] - 0.25) el.currentTime = p.trim[0];
    };
    previewVideo.addEventListener("timeupdate", keepToTrim(previewVideo));
    previewAudio.addEventListener("timeupdate", keepToTrim(previewAudio));

    // Per spec: "just a large text area" — one plain DOM textarea on the grey
    // node body, no wrapper, no overlay label, no unfocused dimming (the dimming
    // plus a low-contrast border is exactly what once made this box invisible).
    const directionInput = document.createElement("textarea");
    directionInput.className = "mmrp-direction";
    directionInput.placeholder = "Prompt — describe the shot…";
    directionInput.spellcheck = false;
    container.appendChild(directionInput);

    node._mmrpBody = {
        root: container,
        uploadButtons,
        fileInputs,
        canvas,
        ctx: canvas.getContext("2d"),
        directionInput,
        previewVideo,
        previewAudio,
        resizeObserver,
    };
    return container;
}

// ---------------------------------------------------------------------------
// Config (reference pack) save/load
// ---------------------------------------------------------------------------

// Configs are FILES ON THE USER'S MACHINE, not server state. Save downloads a .json through
// the browser; Load reads one back through a file picker. The point is portability
// across deployments: a pod can be rebuilt or swapped and the config still opens.
// Nothing here touches /minimax_refpack/packs — the only server call is the input-dir
// listing used to work out which referenced files are actually present on THIS pod.

function configFilename(name) {
    const safe = (name || "config").replace(/[^A-Za-z0-9 ._-]+/g, "_").trim() || "config";
    return `minimax-refpack-${safe}.json`;
}

function buildConfig(node, name) {
    const w = (n) => {
        const widget = widgetByName(node, n);
        return widget ? widget.value || "" : "";
    };
    return {
        version: 1,
        name,
        direction: w("direction"),
        model: w("model"),
        reasoning_effort: w("reasoning_effort"),
        references: toReferencesList(node._mmrpRefs),
    };
}

function downloadJson(filename, obj) {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    // Must be in the document for the click to count as user-initiated in Firefox.
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke on the next tick, not synchronously: Safari aborts a download whose
    // object URL is revoked before it has started reading the blob.
    setTimeout(() => URL.revokeObjectURL(url), 0);
}

function saveConfig(node, anchorBtn) {
    document.querySelectorAll(".mmrp-pack-panel").forEach((p) => p.remove());

    const panel = document.createElement("div");
    panel.className = "mmrp-pack-panel mmrp-save-panel";

    const input = document.createElement("input");
    input.className = "mmrp-save-name";
    input.type = "text";
    input.placeholder = "Config name";
    input.spellcheck = false;
    panel.appendChild(input);

    const saveBtn = document.createElement("button");
    saveBtn.className = "mmrp-btn mmrp-btn-primary";
    saveBtn.textContent = "Download";
    panel.appendChild(saveBtn);

    const status = document.createElement("div");
    status.className = "mmrp-save-status";
    panel.appendChild(status);

    const doSave = () => {
        const name = input.value.trim();
        if (!name) {
            status.textContent = "Name it first.";
            return;
        }
        try {
            const filename = configFilename(name);
            downloadJson(filename, buildConfig(node, name));
            status.textContent = `Downloaded ${filename}`;
            input.disabled = true;
            saveBtn.disabled = true;
            setTimeout(() => panel.remove(), 2000);
        } catch (e) {
            status.textContent = `Download failed: ${e.message}`;
        }
    };
    saveBtn.onclick = doSave;
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doSave();
        else if (e.key === "Escape") panel.remove();
        // Keep graph-level hotkeys (node delete etc.) away from typing.
        e.stopPropagation();
    });

    anchorBtn.parentElement.appendChild(panel);
    input.focus();

    const closeOnOutside = (e) => {
        if (panel.contains(e.target) || e.target === anchorBtn) return;
        panel.remove();
        document.removeEventListener("mousedown", closeOnOutside, true);
    };
    setTimeout(() => document.addEventListener("mousedown", closeOnOutside, true), 0);
}

// The SECOND (and last) JSON.parse in this file. The other is parseRefsValue, which
// guards a widget value; this one guards a file the user picked. Both are wrapped
// because both parse bytes we did not write — an unguarded throw here would surface
// as a dead button, which is the exact class of bug this section already had once.
function parseConfigFile(text) {
    let data;
    try {
        data = JSON.parse(text);
    } catch (e) {
        throw new Error("not valid JSON");
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new Error("not a config object");
    }
    if (!Array.isArray(data.references)) {
        throw new Error("no references list");
    }
    return data;
}

// Which of these filenames actually exist in THIS pod's input dir. Uses the listing
// route the upload pickers already use, one call per kind that appears in the config.
async function missingFiles(references) {
    const kinds = [...new Set(references.map((r) => r.kind))];
    const present = new Set();
    await Promise.all(
        kinds.map(async (kind) => {
            try {
                const res = await fetch(`/minimax_refpack/files?kind=${encodeURIComponent(kind)}`);
                if (!res.ok) return;
                const data = await res.json();
                for (const f of data.files || []) present.add(f);
            } catch (e) {
                // Unreachable listing: report nothing missing rather than flagging
                // every tile red on a transient network blip.
            }
        }),
    );
    if (!present.size) return new Set();
    return new Set(references.map((r) => r.file).filter((f) => !present.has(f)));
}

async function applyConfig(node, data, sourceLabel) {
    const missing = await missingFiles(data.references);
    const list = data.references.map((r) => ({ ...r, missing: missing.has(r.file) }));
    node._mmrpSelected = null;
    applyRefs(node, fromReferencesList(list));

    const directionWidget = widgetByName(node, "direction");
    if (directionWidget && typeof data.direction === "string") {
        directionWidget.value = data.direction;
        if (node._mmrpBody) node._mmrpBody.directionInput.value = data.direction;
    }

    // A config written before these fields existed carries "" — leave the widget alone
    // rather than blanking a valid setting. A model no longer in the dropdown is
    // skipped too: setting a value litegraph cannot offer would show a combo
    // displaying something the user can never pick again.
    const restore = (widgetName, value) => {
        if (typeof value !== "string" || !value) return null;
        const w = widgetByName(node, widgetName);
        if (!w) return null;
        const options = w.options && w.options.values;
        if (Array.isArray(options) && !options.includes(value)) return value;
        w.value = value;
        return null;
    };
    const unavailable = [
        restore("model", data.model),
        restore("reasoning_effort", data.reasoning_effort),
    ].filter(Boolean);

    const problems = [];
    if (missing.size) problems.push(`not on this pod: ${[...missing].join(", ")}`);
    if (unavailable.length) problems.push(`unavailable setting(s): ${unavailable.join(", ")}`);
    if (problems.length) alert(`${sourceLabel} — ${problems.join("; ")}`);
}

function openLoadConfigPanel(node, anchorBtn) {
    document.querySelectorAll(".mmrp-pack-panel").forEach((p) => p.remove());

    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".json,application/json";
    picker.style.display = "none";
    picker.onchange = async () => {
        const file = picker.files && picker.files[0];
        picker.remove();
        if (!file) return;
        try {
            const data = parseConfigFile(await file.text());
            await applyConfig(node, data, file.name);
        } catch (e) {
            alert(`Couldn't load ${file.name}: ${e.message}`);
        }
    };
    document.body.appendChild(picker);
    picker.click();
}

// ---------------------------------------------------------------------------
// Crop/trim editor — ONE modal for both edits, same overlay pattern as the system
// prompt modal. Crop lives on image and video, trim on video and audio; the modal
// shows whichever applies. Everything is edited in fraction/second space and only
// committed on Save; Cancel (or clicking the backdrop) discards.
// ---------------------------------------------------------------------------

const ASPECT_PRESETS = [
    ["1:1", 1],
    ["16:9", 16 / 9],
    ["9:16", 9 / 16],
];

function openEditModal(node, kind, index) {
    const ref = node._mmrpRefs[`${kind}s`][index];
    if (!ref || ref.missing) return;
    mlog("edit_open", { kind, file: ref.file, crop: ref.crop, trim: ref.trim });
    const wantsCrop = kind !== "audio";
    const wantsTrim = kind !== "image";

    let crop = Array.isArray(ref.crop) ? ref.crop.slice() : [0, 0, 1, 1];
    let trim = Array.isArray(ref.trim) ? ref.trim.slice() : null; // null until duration known
    let duration = null;
    let ratio = null; // aspect lock; null = free
    let mediaW = 0;
    let mediaH = 0;
    const r2 = (v) => Math.round(v * 100) / 100;

    // The two Clear buttons dim when there is nothing to clear, so the modal shows
    // at a glance whether this reference currently carries an edit. Live state, the
    // same predicates Save uses — dragging a rect out enables Clear crop instantly.
    let clearCropBtn = null;
    let clearTrimBtn = null;
    const syncClears = () => {
        if (clearCropBtn) clearCropBtn.disabled = normalizeCrop(crop) === null;
        if (clearTrimBtn) clearTrimBtn.disabled = normalizeTrim(trim, duration) === null;
    };

    const overlay = document.createElement("div");
    overlay.className = "mmrp-overlay";
    overlay.onclick = (e) => {
        if (e.target === overlay) overlay.remove();
    };

    const modal = document.createElement("div");
    modal.className = "mmrp-modal mmrp-edit-modal";
    overlay.appendChild(modal);

    const header = document.createElement("div");
    header.className = "mmrp-modal-header";
    header.textContent = `Edit — ${ref.file}`;
    modal.appendChild(header);

    // ---- the media, real pixels via the stock /view route ----
    const stage = document.createElement("div");
    stage.className = "mmrp-edit-stage";
    let media;
    if (kind === "image") {
        media = document.createElement("img");
    } else if (kind === "video") {
        media = document.createElement("video");
        media.muted = true;
        media.playsInline = true;
        media.preload = "auto";
    } else {
        media = document.createElement("audio");
        media.controls = true;
        media.preload = "metadata";
    }
    media.src = fileUrl(ref.file);
    // The media lives in a wrapper so "Play edit" can reframe it (the wrapper becomes
    // the crop's window; the media is scaled and shifted inside it). Untouched, the
    // wrapper shrink-wraps the media and nothing about the layout changes.
    const mediaWrap = document.createElement("div");
    mediaWrap.className = "mmrp-edit-media-wrap";
    mediaWrap.appendChild(media);
    stage.appendChild(mediaWrap);
    modal.appendChild(stage);

    let cropLayer = null;   // set below when the media can be cropped

    // ---- crop rect + corner handles (image/video) ----
    let syncCropRect = () => {};
    if (wantsCrop) {
        const layer = document.createElement("div");
        layer.className = "mmrp-crop-layer";
        const rectEl = document.createElement("div");
        rectEl.className = "mmrp-crop-rect";
        layer.appendChild(rectEl);
        const handles = {};
        for (const corner of ["nw", "ne", "sw", "se"]) {
            const h = document.createElement("div");
            h.className = `mmrp-crop-handle mmrp-crop-${corner}`;
            rectEl.appendChild(h);
            handles[corner] = h;
        }
        stage.appendChild(layer);
        cropLayer = layer;

        syncCropRect = () => {
            rectEl.style.left = `${crop[0] * 100}%`;
            rectEl.style.top = `${crop[1] * 100}%`;
            rectEl.style.width = `${crop[2] * 100}%`;
            rectEl.style.height = `${crop[3] * 100}%`;
            syncClears();
        };
        syncCropRect();

        // Pointer deltas in fractions of the layer box; dragCrop does the math.
        const startDrag = (e, mode) => {
            stopPlayback();
            e.preventDefault();
            e.stopPropagation();
            const box = layer.getBoundingClientRect();
            if (!box.width || !box.height) return;
            const from = { x: e.clientX, y: e.clientY, crop: crop.slice() };
            const move = (ev) => {
                const dx = (ev.clientX - from.x) / box.width;
                const dy = (ev.clientY - from.y) / box.height;
                crop = dragCrop(from.crop, mode, dx, dy, ratio, mediaW, mediaH);
                syncCropRect();
            };
            const up = () => {
                window.removeEventListener("mousemove", move);
                window.removeEventListener("mouseup", up);
            };
            window.addEventListener("mousemove", move);
            window.addEventListener("mouseup", up);
        };
        rectEl.addEventListener("mousedown", (e) => startDrag(e, "move"));
        for (const corner of ["nw", "ne", "sw", "se"]) {
            handles[corner].addEventListener("mousedown", (e) => startDrag(e, corner));
        }
    }

    // ---- aspect presets (crop only): free / the node's width:height / fixed ratios ----
    if (wantsCrop) {
        const row = document.createElement("div");
        row.className = "mmrp-aspect-row";
        const label = document.createElement("span");
        label.className = "mmrp-edit-label";
        label.textContent = "Crop";
        row.appendChild(label);

        const presets = [["Free", null]];
        const wWidget = widgetByName(node, "width");
        const hWidget = widgetByName(node, "height");
        const nodeW = wWidget ? Number(wWidget.value) : 0;
        const nodeH = hWidget ? Number(hWidget.value) : 0;
        if (nodeW > 0 && nodeH > 0) presets.push([`${nodeW}:${nodeH}`, nodeW / nodeH]);
        presets.push(...ASPECT_PRESETS);

        const buttons = [];
        for (const [text, r] of presets) {
            const btn = document.createElement("button");
            btn.className = "mmrp-btn";
            btn.textContent = text;
            btn.onclick = () => {
                stopPlayback();
                ratio = r;
                for (const b of buttons) b.classList.toggle("mmrp-active", b === btn);
                if (r) {
                    crop = setRectAspect(crop, r, mediaW, mediaH);
                    syncCropRect();
                }
            };
            row.appendChild(btn);
            buttons.push(btn);
        }
        // Nothing is highlighted on open. Free IS the starting behaviour (ratio = null),
        // but showing it selected reads as a choice the user made, and then "Clear crop"
        // has nothing left to visibly clear. The highlight means "you picked a lock".

        // Right-aligned, paired with the trim row's Clear trim: back to the whole
        // frame, aspect lock released and every preset unhighlighted. Save then deletes
        // the saved crop, exactly like a hand-dragged full-frame rect would
        // (normalizeCrop -> null).
        clearCropBtn = document.createElement("button");
        clearCropBtn.className = "mmrp-btn mmrp-clear-btn";
        clearCropBtn.textContent = "Clear crop";
        clearCropBtn.onclick = () => {
            stopPlayback();
            mlog("edit_cleared", { file: ref.file, what: "crop" });
            crop = [0, 0, 1, 1];
            ratio = null;
            for (const b of buttons) b.classList.remove("mmrp-active");
            syncCropRect();
        };
        row.appendChild(clearCropBtn);
        modal.appendChild(row);
    }

    // ---- trim bar + 2dp second fields (video/audio) ----
    let syncTrim = () => {};
    if (wantsTrim) {
        const row = document.createElement("div");
        row.className = "mmrp-trim-row";
        const label = document.createElement("span");
        label.className = "mmrp-edit-label";
        label.textContent = "Trim";
        row.appendChild(label);

        const inNum = document.createElement("input");
        const outNum = document.createElement("input");
        for (const el of [inNum, outNum]) {
            el.className = "mmrp-trim-num";
            el.type = "number";
            el.step = "0.01";
            el.min = "0";
            el.disabled = true;
            // Keep graph hotkeys away from typing, same as the save-name box.
            el.addEventListener("keydown", (e) => e.stopPropagation());
        }

        const bar = document.createElement("div");
        bar.className = "mmrp-trim-bar";
        const span = document.createElement("div");
        span.className = "mmrp-trim-span";
        const inHandle = document.createElement("div");
        inHandle.className = "mmrp-trim-handle";
        const outHandle = document.createElement("div");
        outHandle.className = "mmrp-trim-handle";
        bar.appendChild(span);
        bar.appendChild(inHandle);
        bar.appendChild(outHandle);

        const durLabel = document.createElement("span");
        durLabel.className = "mmrp-edit-label";
        durLabel.textContent = "…";

        // "Clear crop"'s pair: back to the whole clip, so Save deletes the saved
        // trim (normalizeTrim collapses a full-span window to null).
        clearTrimBtn = document.createElement("button");
        clearTrimBtn.className = "mmrp-btn mmrp-clear-btn";
        clearTrimBtn.textContent = "Clear trim";

        row.appendChild(inNum);
        row.appendChild(bar);
        row.appendChild(outNum);
        row.appendChild(durLabel);
        row.appendChild(clearTrimBtn);
        modal.appendChild(row);

        syncTrim = () => {
            if (!duration || !trim) return;
            inNum.value = trim[0].toFixed(2);
            outNum.value = trim[1].toFixed(2);
            span.style.left = `${(trim[0] / duration) * 100}%`;
            span.style.width = `${((trim[1] - trim[0]) / duration) * 100}%`;
            inHandle.style.left = `calc(${(trim[0] / duration) * 100}% - 5px)`;
            outHandle.style.left = `calc(${(trim[1] / duration) * 100}% - 5px)`;
            syncClears();
        };

        const setTrim = (start, end, seekTo) => {
            if (!duration) return;
            // in stays >= 0.05s before out; both stay inside the clip
            start = clamp01(r2(start), 0, Math.max(0, r2(duration) - 0.05));
            end = clamp01(r2(end), start + 0.05, r2(duration));
            trim = [start, end];
            syncTrim();
            // scrub the modal's own video so the user sees the frame they chose
            if (kind === "video" && seekTo !== undefined) media.currentTime = seekTo;
        };

        const dragHandle = (which) => (e) => {
            stopPlayback();
            e.preventDefault();
            const box = bar.getBoundingClientRect();
            if (!box.width || !duration) return;
            const move = (ev) => {
                const t = clamp01((ev.clientX - box.left) / box.width, 0, 1) * duration;
                if (which === "in") setTrim(t, trim[1], t);
                else setTrim(trim[0], t, t);
            };
            const up = () => {
                window.removeEventListener("mousemove", move);
                window.removeEventListener("mouseup", up);
            };
            window.addEventListener("mousemove", move);
            window.addEventListener("mouseup", up);
        };
        inHandle.addEventListener("mousedown", dragHandle("in"));
        outHandle.addEventListener("mousedown", dragHandle("out"));

        inNum.addEventListener("change", () => stopPlayback() || setTrim(parseFloat(inNum.value) || 0, trim ? trim[1] : 0, parseFloat(inNum.value) || 0));
        outNum.addEventListener("change", () => stopPlayback() || setTrim(trim ? trim[0] : 0, parseFloat(outNum.value) || 0));
        clearTrimBtn.onclick = () => {
            mlog("edit_cleared", { file: ref.file, what: "trim" });
            stopPlayback();
            setTrim(0, duration || 0);
        };

        media.addEventListener("loadedmetadata", () => {
            duration = media.duration;
            durLabel.textContent = `/ ${r2(duration).toFixed(2)}s`;
            inNum.disabled = false;
            outNum.disabled = false;
            if (!trim) trim = [0, r2(duration)];
            // a saved trim on a since-replaced, shorter file still clamps sanely
            setTrim(trim[0], trim[1], kind === "video" ? trim[0] : undefined);
        });
    }

    // ---- play the original / play the edit (video, audio) -------------------------
    // The tile's ▶ plays the reference; this plays what the SOCKET will carry. "Play
    // edit" seeks to the in-point, stops at the out-point, and (for video) reframes the
    // media so the crop fills the window - the rect overlay is hidden while it runs,
    // because the media underneath it has moved.
    let stopPlayback = () => {};
    if (kind !== "image") {
        const row = document.createElement("div");
        row.className = "mmrp-play-row";
        const label = document.createElement("span");
        label.className = "mmrp-edit-label";
        label.textContent = "Preview";
        row.appendChild(label);

        const origBtn = document.createElement("button");
        origBtn.className = "mmrp-btn";
        const editBtn = document.createElement("button");
        editBtn.className = "mmrp-btn";
        const ORIG = "Play original";
        const EDIT = wantsCrop ? "Play edit" : "Play trim";
        origBtn.textContent = ORIG;
        editBtn.textContent = EDIT;
        row.appendChild(origBtn);
        row.appendChild(editBtn);
        modal.appendChild(row);

        let mode = null;      // null | "original" | "edit"
        let stopAt = null;
        let raf = null;       // out-point watchdog; timeupdate alone fires every ~250ms,
                              // which overshoots the out-point by a visible quarter second.
                              // This is NOT the canvas draw loop the header rules out - it
                              // exists only while the modal's own media is playing.

        const frame = (on) => {
            if (!wantsCrop) return;
            if (on) {
                const box = media.getBoundingClientRect();
                if (!box.width || !box.height) return;
                const f = cropPreviewBox(crop, box.width, box.height);
                mediaWrap.style.width = `${f.wrapW}px`;
                mediaWrap.style.height = `${f.wrapH}px`;
                Object.assign(media.style, {
                    position: "absolute",
                    maxWidth: "none",
                    maxHeight: "none",
                    width: `${f.mediaW}px`,
                    height: `${f.mediaH}px`,
                    left: `${f.left}px`,
                    top: `${f.top}px`,
                });
                if (cropLayer) cropLayer.style.display = "none";
            } else {
                mediaWrap.style.width = "";
                mediaWrap.style.height = "";
                for (const k of ["position", "maxWidth", "maxHeight", "width", "height", "left", "top"]) {
                    media.style[k] = "";
                }
                if (cropLayer) cropLayer.style.display = "";
            }
        };

        const sync = () => {
            origBtn.textContent = mode === "original" ? "Stop" : ORIG;
            editBtn.textContent = mode === "edit" ? "Stop" : EDIT;
        };

        stopPlayback = () => {
            if (!mode) return;
            if (raf !== null) {
                cancelAnimationFrame(raf);
                raf = null;
            }
            media.pause();
            mode = null;
            stopAt = null;
            if (kind === "video") media.muted = true;
            frame(false);
            sync();
        };

        const start = (next) => {
            if (mode === next) {
                stopPlayback();
                return;
            }
            frame(false);
            mode = next;
            if (next === "edit") {
                frame(true);
                media.currentTime = trim ? trim[0] : 0;
                stopAt = trim ? trim[1] : null;
            } else {
                media.currentTime = 0;
                stopAt = null;
            }
            media.muted = false;   // the point of pressing play is to hear it too
            mlog("preview", { file: ref.file, mode: next,
                              from: next === "edit" && trim ? trim[0] : 0,
                              to: next === "edit" && trim ? trim[1] : null });
            media.play().catch(() => stopPlayback());
            const tick = () => {
                if (!mode) return;
                if (stopAt !== null && media.currentTime >= stopAt) {
                    stopPlayback();
                    return;
                }
                raf = requestAnimationFrame(tick);
            };
            raf = requestAnimationFrame(tick);
            sync();
        };

        origBtn.onclick = () => start("original");
        editBtn.onclick = () => start("edit");
        media.addEventListener("timeupdate", () => {
            if (stopAt !== null && media.currentTime >= stopAt) stopPlayback();
        });
        media.addEventListener("ended", () => stopPlayback());
    }

    // For an image, size math only needs the natural dimensions.
    if (kind === "image") {
        media.addEventListener("load", () => {
            mediaW = media.naturalWidth;
            mediaH = media.naturalHeight;
        });
    } else if (kind === "video") {
        media.addEventListener("loadedmetadata", () => {
            mediaW = media.videoWidth;
            mediaH = media.videoHeight;
        });
    }

    // Initial disabled state for the Clear pair — the sync calls above ran before
    // the buttons existed. (Clear trim re-syncs again on loadedmetadata.)
    syncClears();

    // ---- footer ----
    const footer = document.createElement("div");
    footer.className = "mmrp-modal-footer";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "mmrp-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = () => overlay.remove();

    const saveBtn = document.createElement("button");
    saveBtn.className = "mmrp-btn mmrp-btn-primary";
    saveBtn.textContent = "Save";
    saveBtn.onclick = () => {
        const next = cloneRefs(node._mmrpRefs);
        const target = next[`${kind}s`][index];

        const nc = wantsCrop ? normalizeCrop(crop) : null;
        if (nc) target.crop = nc;
        else delete target.crop;

        // the full clip is "no trim" — serialise nothing, like refs.py omits it
        const nt = wantsTrim ? normalizeTrim(trim, duration) : null;
        if (nt) target.trim = nt;
        else delete target.trim;

        mlog("edit_saved", { kind, file: ref.file, crop: nt === null && nc === null ? null : nc,
                             trim: nt, cleared: nc === null && nt === null });
        dropThumb(ref.file); // the tile re-fetches through the route with the edit
        overlay.remove();
        applyRefs(node, next);
    };

    footer.appendChild(cancelBtn);
    footer.appendChild(saveBtn);
    modal.appendChild(footer);

    document.body.appendChild(overlay);
}

// ---------------------------------------------------------------------------
// System prompt modal — the ONLY place `system_prompt` is ever shown. The widget
// itself is hidden on the node body like direction/references_json (onNodeCreated).
// "Load default" only fills the textarea from the server's packaged default; nothing
// commits to the widget until Save & close, so the user can edit from that starting
// point without losing their in-progress edits by opening/closing the modal.
// ---------------------------------------------------------------------------

// Show the Local LLM button only while prompt_provider is `local`. Called from three
// places because a combo can change three ways: the user clicks it, a saved graph
// restores it, or applyPick() sets it from the picker itself.
// REVERSIBLE, unlike hideWidget(): that one installs getter-only accessors and is a
// one-way door, which is right for the three permanently-hidden widgets and wrong here.
// These come back when the provider changes, so the real type is stashed and restored.
// >>> MMRP-VISIBILITY
function setWidgetVisible(w, visible) {
    if (!w) return;
    if (w._mmrpType === undefined) {
        w._mmrpType = w.type;
        w._mmrpDraw = w.draw;      // usually undefined: litegraph draws by type
    }
    try {
        w.type = visible ? w._mmrpType : "hidden";
        w.hidden = !visible;
        if (!w.options) w.options = {};
        w.options.hidden = !visible;
        // [0,0] removes the row from litegraph's layout, which is what lets fixedSize()
        // shrink the node by exactly the hidden rows on the next pass.
        w.computeSize = visible ? undefined : () => [0, 0];
        // Load-bearing, and its absence was a real bug (2026-08-17): zero height keeps a
        // widget out of the LAYOUT but not out of the PAINT. Litegraph kept drawing the
        // hidden ones at their last_y from when they were visible, so `api_base` painted
        // its URL straight over the `reasoning_effort` row. Suppress the paint, and drop
        // the stale coordinate so nothing can be drawn or hit-tested at it either.
        w.draw = visible ? w._mmrpDraw : () => {};
        if (!visible) w.last_y = undefined;
    } catch (_) {
        // Some frontend builds make these reactive accessors. Degrade to "always shown"
        // rather than throwing mid-configure and aborting the workflow load.
    }
}

// reasoning_effort is OpenRouter-only (endpoint.sends_reasoning), so it hides with the
// rest of that group. A dropdown that silently does nothing on `local` is the same trap
// that let a local model id sit in a field OpenRouter then read.
const PROVIDER_FIELDS = {
    openrouter: ["openrouter_api_key", "openrouter_model", "reasoning_effort"],
    local: ["api_base", "local_model_slug"],
};
// <<< MMRP-VISIBILITY

function syncLocalBtn(node) {
    const w = widgetByName(node, "prompt_provider");
    const provider = migrateProviderValue(w ? w.value : undefined);

    const btn = node?._mmrpLocalBtn;
    if (btn) btn.style.display = provider === "local" ? "" : "none";

    // A field the run will ignore is worse than absent: it invites you to fill it in and
    // then silently does nothing with it, which is exactly how a local model id ended up
    // being posted to OpenRouter. `none` hides both groups - it calls nobody.
    for (const [name, fields] of Object.entries(PROVIDER_FIELDS)) {
        for (const field of fields) {
            setWidgetVisible(widgetByName(node, field), provider === name);
        }
    }
    // last_y only settles after litegraph's next layout pass, so re-assert on the frame
    // after rather than reading a stale height now.
    if (node.setSize) {
        setTimeout(() => {
            try {
                node.setSize(fixedSize(node));
                app.graph?.setDirtyCanvas(true, true);
            } catch (_) {}
        }, 0);
    }
}

// litegraph gives a combo widget its own callback; wrapping it is how we hear the user
// changing the value. Guarded against double-wrapping because onNodeCreated can run more
// than once for a node across a paste or an undo, and a chain of wrappers would fire the
// original callback once per wrap.
function watchProviderWidget(node) {
    const w = widgetByName(node, "prompt_provider");
    if (!w || w._mmrpWatched) return;
    const orig = w.callback;
    w.callback = function (...args) {
        const out = orig ? orig.apply(this, args) : undefined;
        syncLocalBtn(node);
        return out;
    };
    w._mmrpWatched = true;
}


// Writes a native widget the way litegraph expects: value first, then its callback, so
// anything watching that widget (serialization, other extensions) sees the change rather
// than a value that appeared behind its back.
function setWidget(node, name, value) {
    const w = widgetByName(node, name);
    if (!w) return false;
    w.value = value;
    if (w.callback) w.callback(value);
    return true;
}


// Answers "how do I know the api_base?" by not making the user answer it. Sweeps the
// known local ports server-side, lists what actually replied, and writes all three
// widgets from one click - provider, URL and model id together, because getting two of
// the three right still fails at queue time with a 404 nobody can read.
function openLocalServerModal(node, anchorBtn) {
    const overlay = document.createElement("div");
    overlay.className = "mmrp-overlay";
    overlay.onclick = (e) => {
        if (e.target === overlay) overlay.remove();
    };

    const modal = document.createElement("div");
    modal.className = "mmrp-modal";
    overlay.appendChild(modal);

    const header = document.createElement("div");
    header.className = "mmrp-modal-header";
    header.textContent = "Local LLM";
    modal.appendChild(header);

    const hint = document.createElement("div");
    hint.className = "mmrp-modal-hint";
    hint.textContent = "Looking for a server on this machine...";
    modal.appendChild(hint);

    const list = document.createElement("div");
    list.className = "mmrp-server-list";
    modal.appendChild(list);

    const applyPick = (base, model) => {
        setWidget(node, "prompt_provider", "local");
        setWidget(node, "api_base", base);
        setWidget(node, "local_model_slug", model);
        syncLocalBtn(node);
        overlay.remove();
        if (anchorBtn) {
            const original = anchorBtn.textContent;
            anchorBtn.textContent = "Using local ✓";
            setTimeout(() => { anchorBtn.textContent = original; }, 2000);
        }
        app.graph?.setDirtyCanvas(true, true);
    };

    const render = (servers) => {
        list.replaceChildren();
        if (!servers.length) {
            hint.textContent =
                "No OpenAI-compatible server answered on this machine. Start LM Studio " +
                "(Developer tab), or `ollama serve`, then Rescan. If your server runs " +
                "somewhere else, type its URL into api_base by hand.";
            return;
        }
        hint.textContent =
            "Pick the model that should write your prompts. Videos will be sent as " +
            "still frames and audio will not be sent at all.";
        for (const s of servers) {
            const group = document.createElement("div");
            group.className = "mmrp-server-group";

            const title = document.createElement("div");
            title.className = "mmrp-server-title";
            title.textContent = `${s.label} — ${s.base}`;
            group.appendChild(title);

            if (!s.models.length) {
                const empty = document.createElement("div");
                empty.className = "mmrp-server-empty";
                empty.textContent = "answering, but no model is loaded";
                group.appendChild(empty);
            }
            for (const m of s.models) {
                const row = document.createElement("button");
                row.className = "mmrp-btn mmrp-server-model";
                row.textContent = m;
                row.onclick = () => applyPick(s.base, m);
                group.appendChild(row);
            }
            list.appendChild(group);
        }
    };

    const sweep = () => {
        hint.textContent = "Looking for a server on this machine...";
        list.replaceChildren();
        apiDetectServers()
            .then(render)
            .catch((e) => { hint.textContent = `Could not scan: ${e.message}`; });
    };

    const footer = document.createElement("div");
    footer.className = "mmrp-modal-footer";

    const rescanBtn = document.createElement("button");
    rescanBtn.className = "mmrp-btn";
    rescanBtn.textContent = "Rescan";
    rescanBtn.onclick = sweep;

    const closeBtn = document.createElement("button");
    closeBtn.className = "mmrp-btn mmrp-btn-primary";
    closeBtn.textContent = "Close";
    closeBtn.onclick = () => overlay.remove();

    footer.appendChild(rescanBtn);
    footer.appendChild(closeBtn);
    modal.appendChild(footer);

    document.body.appendChild(overlay);
    sweep();
}


function openSystemPromptModal(node) {
    const systemPromptWidget = widgetByName(node, "system_prompt");

    const overlay = document.createElement("div");
    overlay.className = "mmrp-overlay";
    overlay.onclick = (e) => {
        if (e.target === overlay) overlay.remove();
    };

    const modal = document.createElement("div");
    modal.className = "mmrp-modal";
    overlay.appendChild(modal);

    const header = document.createElement("div");
    header.className = "mmrp-modal-header";
    header.textContent = "System prompt";
    modal.appendChild(header);

    const hint = document.createElement("div");
    hint.className = "mmrp-modal-hint";
    hint.textContent = "Empty = use the built-in default.";
    modal.appendChild(hint);

    const textarea = document.createElement("textarea");
    textarea.className = "mmrp-modal-textarea";
    textarea.spellcheck = false;
    textarea.value = systemPromptWidget ? systemPromptWidget.value || "" : "";
    modal.appendChild(textarea);

    // A blank widget means "use the packaged default", but an empty box hides WHICH
    // prompt is running. Show it, so the modal is the node's prompt rather than a
    // guess about it. Saving an untouched prefill is a no-op: Save writes the
    // textarea back only if it differs from the default (see saveBtn).
    let prefilledDefault = "";
    if (!textarea.value.trim()) {
        hint.textContent = "Showing the built-in default. Edit to override it for this workflow.";
        apiSystemPromptDefault()
            .then((text) => {
                // Don't clobber anything the user typed while the fetch was in flight.
                if (!textarea.value.trim()) {
                    prefilledDefault = text;
                    textarea.value = text;
                }
            })
            .catch(() => {
                hint.textContent = "Empty = use the built-in default.";
            });
    }

    const footer = document.createElement("div");
    footer.className = "mmrp-modal-footer";

    const loadDefaultBtn = document.createElement("button");
    loadDefaultBtn.className = "mmrp-btn";
    loadDefaultBtn.textContent = "Load default";
    loadDefaultBtn.onclick = async () => {
        try {
            textarea.value = await apiSystemPromptDefault();
        } catch (e) {
            alert(`Couldn't load the default: ${e.message}`);
        }
    };

    const resetBtn = document.createElement("button");
    resetBtn.className = "mmrp-btn";
    resetBtn.textContent = "Reset";
    resetBtn.title = "Discard edits, revert to the last-saved value";
    resetBtn.onclick = () => {
        textarea.value = systemPromptWidget ? systemPromptWidget.value || "" : "";
    };

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "mmrp-btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.onclick = () => overlay.remove();

    const saveCloseBtn = document.createElement("button");
    saveCloseBtn.className = "mmrp-btn mmrp-btn-primary";
    saveCloseBtn.textContent = "Save & close";
    saveCloseBtn.onclick = () => {
        if (systemPromptWidget) {
            // An untouched prefill saves as blank, so the workflow keeps tracking the
            // packaged prompt instead of freezing today's copy into the graph.
            const v = textarea.value;
            systemPromptWidget.value = prefilledDefault && v === prefilledDefault ? "" : v;
        }
        overlay.remove();
    };

    footer.appendChild(loadDefaultBtn);
    footer.appendChild(resetBtn);
    footer.appendChild(cancelBtn);
    footer.appendChild(saveCloseBtn);
    modal.appendChild(footer);

    document.body.appendChild(overlay);
    textarea.focus();
}

// ---------------------------------------------------------------------------
// Selection + delete-key handling. The key handler is CAPTURE-phase and swallows
// the event completely — otherwise ComfyUI's own keydown handler deletes the whole
// NODE on Delete/Backspace. It no-ops while an INPUT/TEXTAREA has focus so
// Backspace in the prompt edits text. Torn down in onRemoved.
// ---------------------------------------------------------------------------

function installSelectionHandlers(node) {
    const body = node._mmrpBody;

    const clearOnOutsideClick = (e) => {
        if (!node._mmrpSelected) return;
        // The canvas's own mousedown decides what a click there means (tile vs
        // empty area); everywhere else, any click drops the selection.
        if (e.target === body.canvas) return;
        node._mmrpSelected = null;
        scheduleDraw(node);
    };

    const keyHandler = (e) => {
        if (!node._mmrpSelected) return;
        const active = document.activeElement;
        if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) return;
        if (e.key === "Delete" || e.key === "Backspace") {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const { kind, index } = node._mmrpSelected;
            removeRef(node, kind, index);
        } else if (e.key === "Escape") {
            node._mmrpSelected = null;
            scheduleDraw(node);
        }
    };

    document.addEventListener("mousedown", clearOnOutsideClick, true);
    document.addEventListener("keydown", keyHandler, true);

    const origOnRemoved = node.onRemoved;
    node.onRemoved = function () {
        document.removeEventListener("mousedown", clearOnOutsideClick, true);
        document.removeEventListener("keydown", keyHandler, true);
        (node._mmrpHideIntervals || []).forEach((id) => clearInterval(id));
        stopPreview(node);
        if (node._mmrpBody) node._mmrpBody.resizeObserver.disconnect();
        liveNodes.delete(node);
        if (origOnRemoved) origOnRemoved.apply(this, arguments);
    };
}

// ---------------------------------------------------------------------------
// Fixed-size enforcement. resizable = false removes litegraph's resize handle;
// the three overrides are belt and braces so NOTHING — a restored workflow, a
// paste, a frontend quirk — can put the node at any other size. This supersedes
// the old "never stomp a restored size" rule: there is no user-chosen size.
// ---------------------------------------------------------------------------

function installSizeGuards(node) {
    node.resizable = false;

    const origOnResize = node.onResize;
    node.onResize = function (size) {
        const f = fixedSize(this);
        size[0] = f[0];
        size[1] = f[1];
        if (origOnResize) origOnResize.call(this, size);
    };

    const origComputeSize = node.computeSize;
    node.computeSize = function () {
        // origComputeSize still runs for its side effects on some frontends, but its
        // answer is discarded — the size is a constant.
        if (origComputeSize) origComputeSize.apply(this, arguments);
        const f = fixedSize(this);
        this.min_size = [f[0], f[1]];
        return [f[0], f[1]];
    };

    const origSetSize = node.setSize;
    node.setSize = function (size) {
        const f = fixedSize(this);
        size[0] = f[0];
        size[1] = f[1];
        if (origSetSize) origSetSize.call(this, size);
        else this.size = size;
    };
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------

app.registerExtension({
    name: "MiniMaxRefPack.RefManager",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const origOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origOnNodeCreated?.apply(this, arguments);
            injectStyles();
            const node = this;

            const refsWidget = widgetByName(node, "references_json");
            node._mmrpRefs = parseRefsValue(refsWidget);
            const ivRefs = hideWidget(refsWidget);

            const directionWidget = widgetByName(node, "direction");
            const ivDirection = hideWidget(directionWidget);

            const systemPromptWidget = widgetByName(node, "system_prompt");
            const ivSystemPrompt = hideWidget(systemPromptWidget);

            // Cleared early in installSelectionHandlers' onRemoved wrapper so a deleted
            // node doesn't leave a hide-poll timer running (they also self-clear after
            // 1s regardless, this just avoids the wait on an early delete).
            node._mmrpHideIntervals = [ivRefs, ivDirection, ivSystemPrompt].filter((id) => id !== undefined);

            const bodyEl = buildCustomBlock(node);
            const domWidget = node.addDOMWidget("mmrp_block", "custom", bodyEl, { serialize: false });
            node._mmrpDomWidget = domWidget;
            // Constant, by construction: the CSS pins the DOM heights this arithmetic
            // assumes (uploadsH/promptH/gap), the canvas height is CANVAS_ROWS.height.
            domWidget.computeSize = () => [CONTENT.width - CONTENT.pad * 2, CONTENT.height];
            liveNodes.add(node);

            node._mmrpBody.directionInput.value = directionWidget ? directionWidget.value || "" : "";
            node._mmrpBody.directionInput.addEventListener("input", () => {
                if (!directionWidget) return;
                directionWidget.value = node._mmrpBody.directionInput.value;
                if (directionWidget.callback) directionWidget.callback(directionWidget.value);
            });

            installSizeGuards(node);
            installSelectionHandlers(node);
            watchProviderWidget(node);
            syncLocalBtn(node);
            renderNodeBody(node);
            syncReferencesWidget(node);

            const origOnConfigure = node.onConfigure;
            node.onConfigure = function (info) {
                const out = origOnConfigure ? origOnConfigure.apply(this, arguments) : undefined;
                // configure() has just applied widgets_values POSITIONALLY, which for any
                // graph saved before 0.3.3 means the values landed in the wrong widgets.
                // The original array is still on `info`, so re-place it by name from the
                // layout it was actually written in.
                const saved = info && info.widgets_values;
                const byName = remapWidgetValues(saved);
                if (byName && detectLayout(saved) !== ORDER_0_3_3) {
                    for (const [name, value] of Object.entries(byName)) {
                        setWidget(this, name, value);
                    }
                    console.log(
                        `[MiniMaxRefPack] event=migrated_layout slots=${saved.length} ` +
                        `provider=${byName.prompt_provider}`
                    );
                } else if (migrateProviderWidget(widgetByName(this, "prompt_provider"))) {
                    console.log(
                        "[MiniMaxRefPack] event=migrated_provider from=use_openrouter " +
                        `to=${widgetByName(this, "prompt_provider").value}`
                    );
                }
                // After the migration above, so a 0.3.1 graph's `true` has already become
                // "openrouter" and the button is hidden rather than flickering on.
                watchProviderWidget(this);
                syncLocalBtn(this);
                const rw = widgetByName(this, "references_json");
                stopPreview(this);
                this._mmrpRefs = parseRefsValue(rw);
                const dw = widgetByName(this, "direction");
                if (this._mmrpBody) this._mmrpBody.directionInput.value = dw ? dw.value || "" : "";
                this._mmrpSelected = null;
                // configure() writes the serialized size straight onto node.size,
                // bypassing our setSize wrapper — re-assert the fixed size.
                this.setSize(fixedSize(this));
                renderNodeBody(this);
                return out;
            };

            // last_y isn't assigned until litegraph's first layout pass — re-assert
            // the fixed size once it exists so the height lands on the real widget top.
            setTimeout(() => {
                node.setSize(fixedSize(node));
                app.graph?.setDirtyCanvas(true, true);
                scheduleDraw(node);
            }, 50);
        };
    },
});
