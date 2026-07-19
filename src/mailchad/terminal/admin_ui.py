"""Minimal HTML admin UI for vault - v3.8.

Server-rendered. Inline SVG chart on overview. Inline-JS search on docs.
No external assets.

Views:
  /admin              - overview (time-series chart) + K_temp TTL config
  /admin/contacts     - list + add form + CSV import + tag filter
  /admin/templates    - list + add form + preview + variable help
  /admin/campaigns    - list + create + launch + scheduled_for
  /admin/entities    - entities + brand/sender config section
  /admin/suppression  - suppression list + CSV export + secrets/TTL config
  /admin/drift        - drift report viewer
  /admin/operators    - team logins (admin-only) + session/auth config
  /admin/docs         - searchable platform docs
"""
from __future__ import annotations

import html as _html
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from mailchad.terminal import db, launch, settings as _settings
from mailchad.terminal.auth import require_session

router = APIRouter()

# design system CSS (embedded, no external assets)
_CSS = """
:root{--font-sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--font-mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;--bg:oklch(0.984 0.002 285);--surface:oklch(1 0 0);--surface-2:oklch(0.975 0.003 285);--surface-3:oklch(0.962 0.004 285);--border:oklch(0.918 0.004 285);--border-2:oklch(0.862 0.006 285);--text:oklch(0.262 0.012 285);--text-muted:oklch(0.505 0.012 285);--text-dim:oklch(0.62 0.01 285);--accent:oklch(0.55 0.205 286);--accent-hover:oklch(0.48 0.205 286);--accent-press:oklch(0.43 0.2 286);--accent-soft:oklch(0.955 0.032 286);--accent-soft-2:oklch(0.925 0.052 286);--accent-border:oklch(0.86 0.07 286);--on-accent:oklch(0.99 0.005 286);--ok-fg:oklch(0.46 0.12 150);--ok-bg:oklch(0.95 0.05 150);--ok-bd:oklch(0.86 0.08 150);--warn-fg:oklch(0.52 0.11 70);--warn-bg:oklch(0.955 0.06 75);--warn-bd:oklch(0.86 0.09 75);--crit-fg:oklch(0.5 0.18 27);--crit-bg:oklch(0.955 0.045 27);--crit-bd:oklch(0.87 0.08 27);--info-fg:oklch(0.5 0.15 250);--info-bg:oklch(0.955 0.04 250);--info-bd:oklch(0.86 0.07 250);--enc-fg:oklch(0.5 0.12 55);--enc-bg:oklch(0.955 0.05 60);--enc-bd:oklch(0.87 0.08 60);--series-1:#4f8ef7;--series-2:#f7a34f;--series-3:#4fc97f;--series-4:#c94f4f;--danger-fg:oklch(0.52 0.2 27);--danger-bg:oklch(0.55 0.19 27);--danger-bg-hover:oklch(0.48 0.19 27);--r-xs:4px;--r-sm:6px;--r:8px;--r-lg:12px;--r-xl:16px;--shadow-xs:0 1px 2px oklch(0.2 0.02 285/0.05);--shadow-sm:0 1px 2px oklch(0.2 0.02 285/0.06),0 1px 1px oklch(0.2 0.02 285/0.04);--shadow:0 4px 12px oklch(0.2 0.02 285/0.08),0 1px 3px oklch(0.2 0.02 285/0.06);--shadow-lg:0 16px 48px oklch(0.2 0.02 285/0.18),0 4px 12px oklch(0.2 0.02 285/0.1);--ring:0 0 0 3px var(--accent-soft-2);--nav-h:52px;--sb-bg:oklch(0.155 0.018 255);--sb-border:oklch(0.21 0.02 255);--sb-text:oklch(0.72 0.02 255);--sb-text-dim:oklch(0.55 0.02 255);--sb-group:oklch(0.42 0.02 255);--sb-hover:oklch(0.21 0.02 255);--sb-active:oklch(0.25 0.06 262);--sb-active-border:oklch(0.6 0.15 286);--sb-scroll:oklch(0.25 0.02 255);--sb-wordmark:oklch(0.95 0.005 260);--sb-wordmark-v:oklch(0.5 0.02 260);--maxw:1160px;color-scheme:light}
[data-theme="dark"]{color-scheme:dark;--bg:oklch(0.178 0.008 285);--surface:oklch(0.212 0.009 285);--surface-2:oklch(0.246 0.011 285);--surface-3:oklch(0.274 0.012 285);--border:oklch(0.305 0.012 285);--border-2:oklch(0.38 0.016 285);--text:oklch(0.945 0.004 285);--text-muted:oklch(0.71 0.012 285);--text-dim:oklch(0.575 0.012 285);--accent:oklch(0.7 0.16 286);--accent-hover:oklch(0.77 0.15 286);--accent-press:oklch(0.82 0.13 286);--accent-soft:oklch(0.3 0.05 286);--accent-soft-2:oklch(0.36 0.07 286);--accent-border:oklch(0.42 0.09 286);--on-accent:oklch(0.16 0.02 286);--ok-fg:oklch(0.82 0.14 152);--ok-bg:oklch(0.32 0.06 152);--ok-bd:oklch(0.42 0.08 152);--warn-fg:oklch(0.84 0.12 78);--warn-bg:oklch(0.34 0.05 70);--warn-bd:oklch(0.45 0.07 70);--crit-fg:oklch(0.8 0.13 27);--crit-bg:oklch(0.34 0.08 27);--crit-bd:oklch(0.46 0.1 27);--info-fg:oklch(0.81 0.11 250);--info-bg:oklch(0.33 0.06 250);--info-bd:oklch(0.45 0.08 250);--enc-fg:oklch(0.83 0.11 65);--enc-bg:oklch(0.34 0.05 60);--enc-bd:oklch(0.45 0.07 60);--series-1:#7aabf9;--series-2:#f9be7a;--series-3:#7ad69a;--series-4:#f97a7a;--danger-fg:oklch(0.78 0.16 27);--danger-bg:oklch(0.5 0.17 27);--danger-bg-hover:oklch(0.56 0.18 27);--shadow-xs:0 1px 2px oklch(0 0 0/0.3);--shadow-sm:0 1px 2px oklch(0 0 0/0.35);--shadow:0 4px 14px oklch(0 0 0/0.45),0 1px 3px oklch(0 0 0/0.3);--shadow-lg:0 20px 56px oklch(0 0 0/0.6),0 6px 16px oklch(0 0 0/0.4);--ring:0 0 0 3px var(--accent-soft);--sb-bg:oklch(0.10 0.012 255);--sb-border:oklch(0.16 0.015 255);--sb-text:oklch(0.68 0.018 255);--sb-text-dim:oklch(0.45 0.015 255);--sb-group:oklch(0.34 0.015 255);--sb-hover:oklch(0.16 0.015 255);--sb-active:oklch(0.20 0.05 262);--sb-active-border:oklch(0.65 0.14 286);--sb-scroll:oklch(0.18 0.015 255);--sb-wordmark:oklch(0.90 0.005 260);--sb-wordmark-v:oklch(0.42 0.018 260)}
.theme-switching,.theme-switching *,.theme-switching *::before,.theme-switching *::after{transition:none !important}
*,*::before,*::after{box-sizing:border-box}*{margin:0}html,body{height:100%;-webkit-text-size-adjust:100%}
body{font-family:var(--font-sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;letter-spacing:-0.006em;display:flex;overflow:hidden}
a{color:inherit}button,input,select,textarea{font:inherit;color:inherit}svg{display:block}
h1,h2,h3,h4{font-weight:600;letter-spacing:-0.018em;line-height:1.2}
::selection{background:var(--accent-soft-2)}
/* Sidebar */
.sidebar{flex-shrink:0;width:220px;height:100vh;position:sticky;top:0;background:var(--sb-bg);border-right:1px solid var(--sb-border);display:flex;flex-direction:column;overflow:hidden;transition:width .18s ease;z-index:60}
body.sb-closed .sidebar{width:56px}
.sb-brand{display:flex;align-items:center;gap:10px;padding:14px 14px 12px;border-bottom:1px solid var(--sb-border);flex-shrink:0;text-decoration:none}
.sb-brand .mark{width:26px;height:26px;flex-shrink:0;border-radius:6px;overflow:hidden;display:block;box-shadow:0 2px 8px oklch(0 0 0/0.4)}

.sb-wordmark{font-size:14px;font-weight:680;color:var(--sb-wordmark);letter-spacing:-0.025em;white-space:nowrap;overflow:hidden;transition:opacity .12s}
.sb-wordmark .v{font-size:10px;font-weight:500;color:var(--sb-wordmark-v);margin-left:4px;letter-spacing:0}
body.sb-closed .sb-wordmark{opacity:0;width:0}
.sb-nav{flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 0;scrollbar-width:thin;scrollbar-color:var(--sb-scroll) transparent}
.sb-group{margin-bottom:2px}
.sb-group-title{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--sb-group);padding:12px 16px 4px;white-space:nowrap;overflow:hidden;transition:opacity .12s,padding .12s}
body.sb-closed .sb-group-title{opacity:0;padding-left:0;padding-right:0}
.sb-item{display:flex;align-items:center;gap:11px;padding:7px 14px;text-decoration:none;color:var(--sb-text);font-size:13px;font-weight:500;white-space:nowrap;transition:background .1s,color .1s;position:relative;cursor:pointer}
.sb-item:hover{background:var(--sb-hover);color:var(--sb-wordmark)}
.sb-item.active{background:var(--sb-active);color:#fff}
.sb-item.active::before{content:'';position:absolute;left:0;top:6px;bottom:6px;width:3px;background:var(--accent);border-radius:0 2px 2px 0}
.sb-item svg{width:16px;height:16px;flex-shrink:0;opacity:.85}
.sb-item.active svg{opacity:1}
.sb-label{overflow:hidden;transition:opacity .12s}
body.sb-closed .sb-label{opacity:0;width:0}
.sb-footer{flex-shrink:0;border-top:1px solid oklch(0.21 0.02 255);padding:10px 8px}
.sb-toggle{width:100%;background:none;border:none;cursor:pointer;color:oklch(0.5 0.02 255);padding:7px 8px;border-radius:var(--r-sm);display:flex;align-items:center;justify-content:center;transition:background .1s,color .1s}
.sb-toggle:hover{background:var(--sb-hover);color:var(--sb-wordmark)}
.sb-toggle svg{width:16px;height:16px}
/* Main area */
.main-wrap{flex:1;min-width:0;display:flex;flex-direction:column;height:100vh;overflow:hidden}
.top-bar{flex-shrink:0;height:48px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;gap:12px}
.top-bar-title{font-size:13.5px;font-weight:600;flex:1;color:var(--text)}
.icon-btn{appearance:none;-webkit-appearance:none;width:32px;height:32px;border-radius:var(--r-sm);border:1px solid var(--border);background:var(--surface);display:grid;place-items:center;cursor:pointer;color:var(--text-muted);transition:background .12s,color .12s,border-color .12s}
.icon-btn:hover{background:var(--surface-2);color:var(--text);border-color:var(--border-2)}.icon-btn svg{width:16px;height:16px}
.signout{color:var(--danger-fg);text-decoration:none;font-size:13px;font-weight:500;display:inline-flex;gap:5px;align-items:center}.signout:hover{text-decoration:underline}
.user-chip{display:inline-flex;align-items:center;gap:8px;padding:3px 10px 3px 3px;border:1px solid var(--border);border-radius:99px;background:var(--surface);font-size:12.5px;color:var(--text-muted)}
.avatar{width:24px;height:24px;border-radius:99px;flex-shrink:0;background:var(--accent-soft-2);color:var(--accent);display:grid;place-items:center;font-size:11px;font-weight:650}
.content{flex:1;overflow-y:auto;padding:28px 24px 80px;max-width:var(--maxw);margin:0 auto;width:100%}
.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:22px;flex-wrap:wrap}
.page-head .titles{min-width:0}.eyebrow{font-size:11.5px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;color:var(--accent);margin-bottom:5px}
h1.page-title{font-size:23px;letter-spacing:-0.025em}.page-sub{color:var(--text-muted);font-size:13.5px;margin-top:4px;max-width:60ch}
.head-actions{display:flex;gap:8px;align-items:center}
.section-title{font-size:13px;font-weight:600;color:var(--text);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title .count{color:var(--text-dim);font-weight:500}
.btn{appearance:none;-webkit-appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 14px;border-radius:var(--r-sm);border:1px solid transparent;font-size:13px;font-weight:550;cursor:pointer;white-space:nowrap;background:var(--surface);color:var(--text);border-color:var(--border-2);transition:background .12s,border-color .12s,color .12s,box-shadow .12s,transform .04s}
.btn:hover{background:var(--surface-2);border-color:var(--text-dim)}.btn:active{transform:translateY(0.5px)}.btn:focus-visible{outline:none;box-shadow:var(--ring)}.btn svg{width:15px;height:15px}
.btn.primary{background:var(--accent);color:var(--on-accent);border-color:transparent;box-shadow:var(--shadow-sm)}.btn.primary:hover{background:var(--accent-hover)}.btn.primary:active{background:var(--accent-press)}
.btn.danger{background:var(--danger-bg);color:#fff;border-color:transparent}.btn.danger:hover{background:var(--danger-bg-hover)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--text-muted)}.btn.ghost:hover{background:var(--surface-2);color:var(--text)}
.btn.sm{padding:5px 10px;font-size:12px;border-radius:var(--r-xs)}.btn.sm svg{width:13px;height:13px}
.btn.block{width:100%}.btn[disabled]{opacity:.5;cursor:not-allowed}
.link{color:var(--accent);text-decoration:none;font-weight:550;cursor:pointer}.link:hover{text-decoration:underline}
.link.muted{color:var(--text-muted)}.link.danger{color:var(--danger-fg)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-sm)}.card.pad{padding:20px}
.card-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px}.card-head h3{font-size:14px}
.card-body{padding:18px 20px}
.grid{display:grid;gap:16px}.cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.cols-3{grid-template-columns:repeat(3,minmax(0,1fr))}.cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}
.form{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);padding:18px 20px;box-shadow:var(--shadow-sm)}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}.field:last-child{margin-bottom:0}
label,.label{font-size:12.5px;font-weight:550;color:var(--text)}.hint{font-size:11.5px;color:var(--text-dim);font-weight:400}
.label-row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
input[type=text],input[type=email],input[type=password],input[type=number],input[type=datetime-local],input[type=search],input[type=file],select,textarea{appearance:none;-webkit-appearance:none;width:100%;background:var(--surface);color:var(--text);border:1px solid var(--border-2);border-radius:var(--r-sm);padding:8px 11px;font-size:13.5px;transition:border-color .12s,box-shadow .12s,background .12s}
input::placeholder,textarea::placeholder{color:var(--text-dim)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:var(--ring)}
.input{appearance:none;-webkit-appearance:none;width:100%;background:var(--surface);color:var(--text);border:1px solid var(--border-2);border-radius:var(--r-sm);padding:8px 11px;font-size:13.5px}
.input.sm{padding:5px 9px;font-size:12.5px}
.banner{padding:9px 13px;border-radius:var(--r-sm);font-size:12.5px;border:1px solid var(--border-2)}
.banner.ok{background:var(--ok-bg,#eaf7ee);border-color:var(--ok-fg,#3a8);color:var(--ok-fg,#176)}
textarea{min-height:8em;resize:vertical;line-height:1.5}textarea.mono,input.mono{font-family:var(--font-mono);font-size:12.5px;letter-spacing:-0.01em}
select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 9px center;padding-right:30px;cursor:pointer}
input[type=file]{padding:6px;cursor:pointer}
input[type=file]::file-selector-button{font:inherit;font-size:12.5px;font-weight:550;margin-right:10px;cursor:pointer;background:var(--surface-2);color:var(--text);border:1px solid var(--border-2);padding:5px 11px;border-radius:var(--r-xs)}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-grid .span-2{grid-column:1/-1}
.form-actions{display:flex;gap:8px;align-items:center;margin-top:4px}fieldset{border:none;padding:0}
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);overflow-x:auto;box-shadow:var(--shadow-sm)}
table.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl thead th{text-align:left;font-weight:550;font-size:11.5px;letter-spacing:0.02em;text-transform:uppercase;color:var(--text-dim);padding:10px 14px;background:var(--surface-2);border-bottom:1px solid var(--border);white-space:nowrap}
.tbl tbody td{padding:11px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
.tbl tbody tr:last-child td{border-bottom:none}.tbl tbody tr{transition:background .1s}.tbl tbody tr:hover{background:var(--surface-2)}
.tbl td.num,.tbl th.num{text-align:right;font-variant-numeric:tabular-nums}
.tbl td.actions{text-align:right;white-space:nowrap}.tbl .row-actions{display:inline-flex;gap:4px;justify-content:flex-end;align-items:center}
.more-menu-item{display:block;width:100%;padding:7px 14px;font-size:12.5px;text-align:left;background:none;border:0;color:var(--text);cursor:pointer;text-decoration:none;white-space:nowrap}.more-menu-item:hover{background:var(--surface-2)}
.cell-strong{font-weight:550;color:var(--text)}.cell-mono{font-family:var(--font-mono);font-size:12px;color:var(--text-muted)}.cell-dim{color:var(--text-muted)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:550;line-height:1.5;background:var(--surface-3);color:var(--text-muted);border:1px solid var(--border)}
.pill .dot{width:6px;height:6px;border-radius:99px;background:currentColor;flex-shrink:0}
.pill.ok{background:var(--ok-bg);color:var(--ok-fg);border-color:var(--ok-bd)}.pill.warn{background:var(--warn-bg);color:var(--warn-fg);border-color:var(--warn-bd)}.pill.crit{background:var(--crit-bg);color:var(--crit-fg);border-color:var(--crit-bd)}.pill.info{background:var(--info-bg);color:var(--info-fg);border-color:var(--info-bd)}.pill.enc{background:var(--enc-bg);color:var(--enc-fg);border-color:var(--enc-bd)}.pill.accent{background:var(--accent-soft);color:var(--accent);border-color:var(--accent-border)}
.tag{display:inline-flex;align-items:center;padding:1px 7px;border-radius:var(--r-xs);background:var(--surface-3);color:var(--text-muted);font-size:11px;font-weight:500;border:1px solid var(--border);font-family:var(--font-mono);letter-spacing:-0.01em}
.tags{display:inline-flex;flex-wrap:wrap;gap:4px}
.key-badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:550}
.key-badge.set{background:var(--ok-bg);color:var(--ok-fg)}.key-badge.unset{background:var(--crit-bg);color:var(--crit-fg)}
.stat-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;box-shadow:var(--shadow-xs)}
.stat .k{font-size:11.5px;color:var(--text-muted);font-weight:550;letter-spacing:.01em;display:flex;align-items:center;gap:6px}
.stat .v{font-size:25px;font-weight:600;letter-spacing:-0.03em;margin-top:6px;font-variant-numeric:tabular-nums}
.stat .sub{font-size:12px;color:var(--text-dim);margin-top:2px}
.stat .v.ok{color:var(--ok-fg)}.stat .v.warn{color:var(--warn-fg)}.stat .v.crit{color:var(--crit-fg)}.stat .v.info{color:var(--info-fg)}.stat .v.accent{color:var(--accent)}
.stat .led{width:7px;height:7px;border-radius:99px}
.dim{color:var(--text-dim);font-size:12.5px}.muted{color:var(--text-muted)}.mono{font-family:var(--font-mono)}
.row{display:flex;align-items:center;gap:10px}.row.wrap{flex-wrap:wrap}.between{justify-content:space-between}
.gap-sm{gap:6px}.gap-lg{gap:18px}.stack{display:flex;flex-direction:column}.spacer{flex:1}
.mt{margin-top:16px}.mt-lg{margin-top:24px}.mb{margin-bottom:16px}
.divider{height:1px;background:var(--border);margin:18px 0;border:none}.nowrap{white-space:nowrap}
.callout{display:flex;gap:11px;padding:12px 15px;border-radius:var(--r);background:var(--accent-soft);border:1px solid var(--accent-border);font-size:13px;color:var(--text)}
.callout.warn{background:var(--warn-bg);border-color:var(--warn-bd);color:var(--warn-fg)}.callout.crit{background:var(--crit-bg);border-color:var(--crit-bd);color:var(--crit-fg)}.callout svg{width:17px;height:17px;flex-shrink:0;margin-top:1px}.callout .ct-body{min-width:0}
.empty{text-align:center;padding:48px 20px;color:var(--text-dim)}.empty svg{width:30px;height:30px;margin:0 auto 12px;color:var(--border-2)}.empty h4{color:var(--text-muted);font-size:14px;margin-bottom:4px}
.modal-overlay{position:fixed;inset:0;z-index:100;background:oklch(0.2 0.02 285/0.45);backdrop-filter:blur(2px);display:none;align-items:center;justify-content:center;padding:24px}
.help-overlay{position:fixed;inset:0;z-index:110;background:oklch(0.2 0.02 285/0.35);backdrop-filter:blur(2px);display:none;align-items:flex-start;justify-content:center;padding:48px 24px;overflow-y:auto}
.help-overlay.open{display:flex}
.help-card{width:100%;max-width:560px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-lg);overflow:hidden;animation:pop .15s ease}
.help-card pre{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-sm);padding:12px 14px;font-size:12px;font-family:var(--font-mono);overflow-x:auto;line-height:1.5;margin:8px 0}
.help-card .field-table{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0}
.help-card .field-table th{text-align:left;padding:5px 8px;background:var(--surface-2);font-weight:600;color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.help-card .field-table td{padding:6px 8px;border-top:1px solid var(--border);vertical-align:top}
.help-card .field-table .req{color:var(--ok-fg);font-size:10px;font-weight:700}
.modal-overlay.open{display:flex}
.modal{width:100%;max-width:460px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);box-shadow:var(--shadow-lg);overflow:hidden;animation:pop .16s ease}
@keyframes pop{from{opacity:0;transform:translateY(8px) scale(0.98)}to{opacity:1;transform:none}}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}.modal-head h3{font-size:15px}
.modal-body{padding:20px}.modal-foot{padding:14px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px;background:var(--surface-2)}
.doc-section{padding:18px 0;border-bottom:1px solid var(--border)}.doc-section h2{font-size:15px;margin-bottom:6px}.doc-section p{color:var(--text-muted);font-size:13.5px;max-width:70ch}.doc-section.hidden{display:none}
.setting-row{display:grid;grid-template-columns:minmax(160px,240px) 1fr;gap:18px;padding:16px 0;border-bottom:1px solid var(--border);align-items:start}
.setting-row:last-child{border-bottom:none}.setting-row .sr-label{font-weight:550;font-size:13px}.setting-row .sr-desc{color:var(--text-dim);font-size:12px;margin-top:3px}
.setting-row .sr-control{display:flex;flex-direction:column;gap:8px}
.setting-row .sr-control .row{flex-wrap:wrap}
code,.code{font-family:var(--font-mono);font-size:.92em;background:var(--surface-3);padding:1px 5px;border-radius:var(--r-xs);border:1px solid var(--border);color:var(--text-muted)}
*{scrollbar-width:thin;scrollbar-color:var(--border-2) transparent}
*::-webkit-scrollbar{width:9px;height:9px}*::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:99px;border:2px solid var(--bg)}
"""

# icons (lucide-style)
_ICONS = {
    "overview":    '<path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/>',
    "contacts":    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "templates":   '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M9 21V9"/>',
    "campaigns":   '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    "entities":    '<path d="M3 21h18"/><path d="M5 21V7l8-4v18"/><path d="M19 21V11l-6-4"/><path d="M9 9h.01M9 13h.01M9 17h.01"/>',
    "suppression": '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    "drift":       '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "docs":        '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    "operators":   '<path d="M18 21a8 8 0 0 0-16 0"/><circle cx="10" cy="8" r="5"/><path d="M22 20c0-3.37-2-6.5-4-8a5 5 0 0 0-.45-8.3"/>',
    "schedule":    '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/>',
    "quicksend":   '<path d="m22 2-7 20-4-9-9-4 20-7z"/><path d="M22 2 11 13"/>',
    "tags":        '<path d="M12 2H2v10l9.29 9.29a1 1 0 0 0 1.41 0l6.29-6.29a1 1 0 0 0 0-1.41L12 2z"/><circle cx="7" cy="7" r="1"/>',
    "audit":       '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>',
    "seeds":       '<path d="M7 20c4 0 7-3 7-7V5h-4a7 7 0 0 0-7 7c0 4 3 7 4 8z"/><path d="M7 20c0-4 2-7 6-9"/>',
}

def _icon(name: str, cls: str = "") -> str:
    d = _ICONS.get(name, "")
    c = f' class="{cls}"' if cls else ""
    return f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"{c}>{d}</svg>'

# inline settings helper

def _setting_row(key: str, label: str, *,
                 is_secret: bool = False, kind: str = "text", help: str = "") -> str:
    current = _settings.get(key)
    if is_secret:
        present = bool(current and current != "<encrypted>")
        value_display = (
            f'<span class="pill enc">encrypted</span> '
            f'<a class="link" style="font-size:12px" href="/admin/settings/reveal/{key}">reveal</a>'
            if present else '<span class="pill">not set</span>'
        )
        inp = f'<input type="password" name="value" placeholder="paste new value to replace" autocomplete="off" style="flex:1;min-width:0">'
    else:
        value_display = (
            f'<code class="code">{_html.escape((current or "")[:80])}</code>'
            if current else '<span class="pill">not set</span>'
        )
        inp = f'<input type="{kind}" name="value" value="{_html.escape(current or "")}" style="flex:1;min-width:0">'
    help_html = f'<div class="hint" style="margin-top:2px">{help}</div>' if help else ""
    return f"""<div class="setting-row">
  <div>
    <div class="sr-label">{label}</div>
    <code class="code" style="font-size:11px;margin-top:3px;display:inline-block">{key}</code>
    {help_html}
  </div>
  <div class="sr-control">
    <div>{value_display}</div>
    <form method="post" action="/admin/settings/{key}/save" style="display:flex;gap:8px">
      {inp}
      <button class="btn sm" type="submit">Save</button>
    </form>
  </div>
</div>"""


def _config_section(title: str, rows_html: str) -> str:
    """Collapsible config card (setting-rows) for embedding in feature pages."""
    chev = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>'
    return f"""<details class="card" style="margin-top:28px">
  <summary style="padding:14px 20px;cursor:pointer;font-weight:600;font-size:13.5px;list-style:none;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
    ⚙ {title} {chev}
  </summary>
  <div style="padding:0 20px 4px">{rows_html}</div>
</details>"""


def _config_block(title: str, content_html: str) -> str:
    """Collapsible config card (free-form HTML) for embedding in feature pages."""
    chev = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>'
    return f"""<details class="card" style="margin-top:16px">
  <summary style="padding:14px 20px;cursor:pointer;font-weight:600;font-size:13.5px;list-style:none;display:flex;align-items:center;justify-content:space-between">
    ⚙ {title} {chev}
  </summary>
  <div style="padding:12px 20px 18px">{content_html}</div>
</details>"""


# layout

def _layout(title: str, body: str, current: str = "", session: dict | None = None) -> HTMLResponse:
    is_admin = (session or {}).get("role", "admin") == "admin"
    sub = (session or {}).get("sub", "")
    initials = "".join(p[0].upper() for p in sub.split("@")[0].replace(".", " ").split()[:2]) if sub else "?"

    # Setup-flow order, grouped like AWS console
    NAV_GROUPS = [
        ("Getting started", [
            ("overview",   "Overview",    "/admin"),
            ("entities",   "Entities",    "/admin/entities"),
            ("quicksend",  "Quick send",  "/admin/quicksend"),
        ]),
        ("Content", [
            ("templates",  "Templates",   "/admin/templates"),
            ("contacts",   "Contacts",    "/admin/contacts"),
            ("campaigns",  "Campaigns",   "/admin/campaigns"),
            ("tags",       "Tags",        "/admin/tags"),
        ]),
        ("Operations", [
            ("schedule",    "Schedule",    "/admin/schedule"),
            ("suppression", "Suppression", "/admin/suppression"),
            ("seeds",       "Seeds",       "/admin/seeds"),
            ("drift",       "Drift",       "/admin/drift"),
            ("audit",       "Audit log",   "/admin/audit"),
        ]),
        ("System", (
            ([("operators", "Operators", "/admin/operators")] if is_admin else []) +
            [("docs", "Docs", "/admin/docs")]
        )),
    ]

    def _sb_item(key, label, href):
        active = " active" if href == current else ""
        return (f'<a class="sb-item{active}" href="{href}" title="{label}">'
                f'{_icon(key)}<span class="sb-label">{label}</span></a>')

    nav_html = ""
    for group_title, items in NAV_GROUPS:
        if not items:
            continue
        nav_html += f'<div class="sb-group"><div class="sb-group-title">{group_title}</div>'
        for key, label, href in items:
            nav_html += _sb_item(key, label, href)
        nav_html += '</div>'

    mail_svg  = '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAb7UlEQVR42sV7a2xc55ne833nMmdmzsxwOBdqSCmSGEqiSEqRI18iObKluErS2HB3HUjI2o43QgMDdREnAbYBtjeKRYqmP5pkt6mRNbIwnBhOSytpE6+zG8swSSe2INO1LStSHUkWKYriReTMcC7nzJlz+/ojfL89ZOTYcRp0gIGtGc7MeW/P+7zP9x6GP+AhhGDHjx9XRkZGQgBh9D3DMNBqtQpLS0sb5+bmsmEY8lqtZk5PT2+anp7eury83O04TrfruiXbtrNCCFVVVaiq2gZgAxCMMZFMJqulUuntnTt3vnL33Xf/slQqvdVsNuXvHDlyRBkdHRWMsfCD2MA+qPGjo6PK0aNHAwDgnCMIgvT09PSHJycnd01NTe29fPnyTSsrK/3Ly8u5IAh4EARwHAf1eh2tVgue58H3fYRhCM45OOfQNA2KogAAXNdFEARQVRWGYaCzsxNdXV3u1q1bz/b394997GMf+/u9e/e+xhirA8Dw8LAKIFwNxh/PAVHDhRDm+Pj4n42Pj//p+fPnb5qfn9+wsrICejqOA8YYgiAQqqrC932RSqVELBZDGIbQdZ0xxpimadA0DaqqIgxD+L4PIQR0XQdjTAAQrVaLBUHAASCVSqFUKqGrq2vu5ptvfvGrX/3qf2KMnadrHB4eVo8fPx6sfvb/jQOEEIwxxgCEQghlcnLyz3/0ox/95cTERN/FixfRbrcRBAEURQmEEEJRFBaLxbjv+0zXdXoP6XQaruuCcw7P8xCPx5FIJBCGIRzHged5UBQFiqIgk8nA933U63W0221kMhnBGBP1ej30PI/7vs87Ozvx6U9/unH48OH/fPfdd/8P0zQvWZb1W8H6gxywarxgjOFXv/rVnzz99NP/dmJiYu+ZM2fg+75vmibjnHNVVZnneRBCYDXiUFUVjDG0Wi3kcjn5nb7vw7ZtFAoFhGEIwzBQr9fxGx8DPT09EEJgfn4ejuOgo6MDQRCg2WxSZoBzLmq1WhAEgdrf34/u7u72pk2bzu/Zs+eFBx544FuMsfnh4WF1ZGTE/8AOWAU6dvz4cfWJJ5747okTJ45NTEwgDMPANE2maRqn6BuGAcdxoOs6NE2D53nQdR2NRkM6gnMOIQR830cymYQQApqmgXMO3/cRj8dRKBRgGAauXbsGx3GQTqdhWRbCMEQ2m0W73Uaj0QDhD+dcCCFCAArnHF1dXfjkJz85+/DDD3++VCqN/y4nKO/lgMHBQeXRRx8NBwYGvvf4448fe+mll/x0Og3TNBXP85iiKLJm6REEAVzXlZEOggCMMRndMAyhaRoAIBaLgb4jkUgglUrJtCcQZIxJsLQsC81mE0II6UwhBOOc8zAMRbvdFouLi/4bb7yRXVxc/Pxjjz228OCDD04ODw+rExMT4e+VAVRDzz///JHvfve7oy+//LKbTCY113UZpbVhGGCMSYOFEAjDEEIIMMZkPXPOoev6mtfpNV3XoarqbzmMAFFRFLiuKx1N3xmGITzPQ6vVgu/7sv3qug7f98NarYZ77rmHHzt27NG77rrrv94IE9R3M354eJgfPXoUQoiOL3/5y391+vTpMJfLqa7rMkVR4HmeNF5RFBiG8Y9ejRiuqqpsb1QCZHAYhjILyOAgCOB5HjzPQxiG8hmLxWAYBnzfh+/7cF1XOoUMZ4zJv9M0jafTaXHy5ElfUZS/Hh8fXzh48OAz652gvgfiB0899dQ3X3755VI8Hg96enoUxhhs20YQBLKPR1ObjAQgnUDRAgBd10Ft0PM8aJoGXdfBOZdO8TwP7XYbruuCnO37PjzPwypZgqIo0HVdvkZcgvBltXxYPp/nv/jFL8SHP/zhvxFCPH/8+PEGgfq7OYAxxpiqqsGzzz478p3vfOfY0tJSsHv3biWVSkEIAdM00W63JTABAKE/OYMAijCBsoEMAgBN02TECCMIFA3DgOd5kjy5roswDEEtjiK/SsIkrpBTHMdBEATQdZ27rutPTk5mT548+eWRkZH/sGq3f0MHDA8Ps+PHj+P73//+U9/+9rcfOHfuXLB//34lmUzCdV3pcQAyE25U+xQNigx9hoCNjI/FYvKz5AD6nKqqMmMcx5EpT78TBIEEX+oivu+Dcy6/y7ZtCCGUkydPhps3b/5LIcQoY+xtIQRnjIXKOuPVkZGR4FOf+tTnf/jDH/77iYkJb//+/Womk5E1TC3P931JaaMdgCJJ0eacy8+SYRRh6ueURfS35BByLBGnVVYp36NyME0T6XQa6XQasVhMOo5+PxaLMUVRwtnZWV0I0fvCCy88HYYhm5iYEOszIOSc46233nr47Nmz4c0338w7OzsRhiEURYHjOJKxUaSIu1M0KAKUmlT/5CQyXFVVWQpkdNSJFFFK5TAMkUgkZIfwPE9mE80OFAwKEGUnYwzJZFK5fv16ODk5+RnP83Yzxt4aHR1VeBT1R0ZGwiAIet955529sViM9fb2ckVREI/HYVkWXNeVUXddF+12W2ZCu91Gu92WqUwoTp+h1KX6pc8SuEVnAIocRdwwDJimKVsmZYumaRBCSMCkayLHx2IxcM7lNRmGEdZqNZw6depTAFihUGDSAQcPHuRCCDY5OXnnlStXjI0bNwadnZ2MWk90ehNCwHEc+QOUuvRfmU6rP0xkh9Caviua9jcqFzJGVVWJLalUCqZpIplMyqeqqrJ9CiFkgAAgkUhI4I3FYiiXy5idnf0YAPHYY4/9YwmMj4/j0KFD4sSJEzcHQYBt27bJaJEXKUK+70PXdZimiVqttga4iANQShPNpfdpPoiCJTmLGKGmaUgkEhJHLMuSZUCGENbQv1fJj8QLKgnChFXHKwsLC3jnnXcOCCHSjLG6Gun7vhAi/Y1vfOOfua6LdDrNr127Bs/zkEwm4XkeISo450in07IXt1ot+ToRFzI6WhZRFqfrOuLxODKZDFRVlSVBNb0KXjAMA9lsFtVqFdVqFUEQrOn35Mj13YAcQqRqtfOwZrMZXrhwoTAzM3MrgBdUAHjmmWc4gODq1av7Z2dne+LxeGjbNqcPN5tNqKqKSqWCRCKB7u5ueRHxeBy1Wg2tVktmAF0IpSIBZBiGSKVSEhgVRcHKyorsCsT7o1hAiF4sFpFOp9FsNlGv19fMHNHOoWma/D2i0HQtq+UZLi8vs+np6SHpgEKhwABgeXm5JwgCUSgUQkVReCqVwuLioqwloqrxeBzXr19Hu90G51zWKqUdgR/hBaU8gR+ldq1WQywWQyKRgGmaSCQSSCaTawCQniSadHR0QNM0uK4rOwSVUJQQUblQVlKZBkEA27aZbdsfWkOERkdHFc/z/I6ODkZEhVSaRqOBXC6HdDqNRqOBqampNSXhuq4kHeSMKPUlxCY8oO9VFAVBEMCyLNlNaEzWNE0OQtT6qAOk02kwxrC4uIharSYdQSVEWRCh9tE2y6rVKur1ei8AqKvzPh8ZGXFffPHFQcMwJM21bRuO42B+fh4AkMvlUKvVsLi4KHtys9mE67oSlKgLkCZAEaa6X4/49D2u68q2Zts2DTS/xTBpXiAlqdVqyYyjVI+SqCi3iAJxGIZZAFBXhwL32rVrf/rkk08+MjU1FWYyGWV6ehrVahW2bcO2bdnK0um0VHMIwQmshBCwLGvNrE79mcCRGJymaUgmk8hkMkgkEnJAymazMp3J2OiUSACraRpM00Sj0ZCkiOqcOldUZG21Wkgmk2i1Wmu6jyqE2PDjH//4m9/61rf+7MyZM2CMiTfffJNRVCk92+02Ojs7sWHDBhklzjkMw5ARrdVq8H1ftie6MIpgPB5HMpmUIOU4DsrlMpLJJDo7O6FpGhzHQSKRQCaTWVNK0SwxDEOWRTweR6PRWJPyVDY0UOm6jkKhgI6ODkxNTYkPfehDKBaLvxRCMPUnP/nJ3544ceIzZ8+eDWKxGC+Xy4x096iK43kecrkcent7YVkWYrEYOjs7wRhDo9FAuVxGu91ew9+jpMgwDKRSKTkAGYaxJjPoOzVNQzweh+u6sr+TE6IlRq2VyoxkN4p6Op2WBKmnpwemaeLtt99GIpHAzp07kc/nLzPGhLqwsHDLpUuXAl3XuWVZjDEm6zI6pJACFIvFkEwm4TiORG5N09But1Eul9eAIhlAtJRGXeIHRFCiKB+LxdaM08TuCDuom1ApkBhDUpqu6+jp6UE8HpejexiG2LRpExhjePXVV/krr7yCoaGhfyGEeFoNw1CNxWKcMYahoSGEYYhz586hWq3KHk7/DYIAy8vLsG1bepeiTshOkSKDXdeVf0sARBNgFCjj8biMYHS4IaCMBiJKl+n9MAxh2zY0TUMul5MDVxAEmJ+fR71eR7FYxK5du/jk5KR488039x4+fPifqOl0ei6Xy2UNw/CFEOobb7yBcrm8RlRIpVJot9vo7+9HuVyWbZFSmPAik8lI3IgKoVEkD8MQqqpKo3Vdl4hPT0rzaMskY6mXU2kqiiL5SFQt0jRNBqJYLKJer6NcLsMwDHR0dIS//vWv+YULF25Th4aG/uLixYv/6/Tp07GFhQVx7do1xhhDd3c3FhYWZHvJZDLo7OzEwsICDMNY84N0cR0dHTLl2+32mnGX0J+4OaVuFBPIeOILlE1U82QotcSoc4MgQKvVIqIjs+XSpUtrskEIge7ubra0tMQuXrx4n7pnz55/mJycvMM0zadOnTq1rVKphKZp8mKxKNnaysoKdu7cKUuBBhUyjnMO0zTRbDZhGAbi8bic9ijdCQciIoW8sKhoSu9HmRsBHn0n4YHrumg0Gmi1WvL0KJfLQdM01Go1ZDIZNJtNXL9+HYRtq7/LstkshBBZZWxsTL399tuvPv/88/9QLpf/PAiC2MDAAIQQbH5+Ho1GA4lEAoODg6hUKjAMA4lEQgobdMGpVAq6rqPZbEo2GFWHKQuIzFBkSfKiqEfFjCiDi1JearO+76PZbKJSqUgs2rhxI/L5PCzLAucc+XweHR0dsG1bOmtpaUnk83l26NChSX7o0CF/dHRU4ZxfzGazc/F4nE1NTYm5uTkIIZBMJjE0NISVlRX4vi/TNqrrqaoqabDv+2g0GrIEovJ2NKpRUcWyLFiWJUuG0j2qG6yX1qkrkeFElSko1KFyuRwymQyy2azsXpZliWw2i46OjndU0slff/31w6+99tq2SqUSWpbF2+02DMNAb28vMpkMpqenpfhA6E4XTeMwzfG5XA7Xr1+XogSxuqjgQRlALZOkcMqIWCwmjV9/skTnjo7jrDmVIh2AnEhiaiaTQS6Xw+LiIoIgwObNm1EsFoXv+1W1Wq1yAMGVK1c+HYah0tvb68/MzHBK2927d+PcuXMyJTnnsmVFGWI6nZYMkdoXSdSU1jRVEpMj1CcgazabcpCh0qLsaTabslRqtZocxOiEWdd1uK6LWq2Ger2OZDIJy7IwNzcHx3EQj8dRKpVQKpUoi1gul7uizs3NBatn7nsqlQpisRgj/T+qAkUlpzAM0Wq10Gg0UK1W0Ww20dHRgb6+PjiOI3UDUndIs6PBJZ/Py95NqK8oCpaWliSdpnKh6dDzPNTrdaRSKXieJ2eAeDwuCVOr1ZKl2NnZKfFB13Xk83nYto1WqxW2Wi3W29vb3L1797PKxMSEmJ2d3VQul79pWRYPw5D5vs8sy8KlS5eQz+flCS+pwa1WS57cOo4jU3tpaQmzs7NrACw6p1PEGo2GNIyk7FXlFsvLy2g2mxIISYyl9KbSo981TROcc/mdnHNZ70EQIJlMIpvNUlYGiUQCn/jEJ5Rbb731kU2bNo2rAJiiKJVkMjm3devWTe12W0xNTbHZ2VksLS1hcnISpmnKC0wmk6hWq3LgoMOKRqMhkTda51HEp1oVQqBcLqNer6NUKqG3t1dmSjqdxtWrVxGGIZLJJACgXq/Lz0aZZ1dXlwRCAkbGmGx9q/wkZIwJ0zSVQqGgDA0NYXBw8NE9e/b87ejoqKKOjY0ppVLJeumll/4GwH/MZDKB67q80WhA13VcuXIFAwMD2LBhAyqVCorFIhYXFzEzMyNbV/Qcj+qWLoi0OSoneSi5Skymp6ehKAp27Ngh05pOm2lxgnOO5eVlJBIJKYpET5qjErmu67Asi2RwUSgUeFdXF3K5nLt9+/bLO3fu/IstW7Y8R+CvHjx4MACAAwcO/Lcnnnjiq7Zt55PJpOjp6WHNZhPFYhGJRAJzc3Ny8ovs+MjeTawsOgeQpkfgSeNplCcAQKVSkRdNXSF63NZut2XG0TxBbJN+J8oPHMeBYRhicHCQffSjH31148aNf7Vv375JANOMMS96QqwyxsSRI0cUxljt8ccfPz09PX23aZpBNptVPc9DPp+XLadQKGBlZQVLS0uypkndpQuMSmkUGZLXLcuSsztFmi6chFXCDuoelC3UlXzfRyqVks7TNA3NZlO2O1VVsbCwEIZhyG655Za377vvvjsZY86NlrwAgAPAI488wgCwUqk0RiDneR4SiQRc10W9XpcnuKqqYsOGDSgWiyI6zhIARcXPIAhQq9Vkt1hYWMD8/DyazaZsb0RabNuWHSV6UhQVVYiHkFTnOI5snfl8PjpShx0dHUzX9V8yxpyf/exnseHhYS6EYOsXJPjqqVAIQNxzzz0/zWQy7ZWVFUVVVdHV1SWjODAwgG3btsE0TUpfFu3D3d3dME0TO3fuxI4dO9DV1YX9+/ejVCohl8vJMZgYGpUMCaBUPmRw9NyRWiVlEnF/orZRDTAWiyGdTqPdbsNxnDgAdvr06WBkZCS80docqcL0xjVN06ozMzMbMpmMoDMBOge4cOECGo0Gstmsv2vXrpXp6ek8AGGaJtu+fTtmZ2clGDabTbz99tuyXSqKgkKhAMuyUKvV1kx1UfYWPSKnDKE+v7S09FvzRSqVkpyC+Imu67xaraJSqQxpmiZGRkbedVVu/elwljGWWlhYkNFoNBo4c+YMzp49i2vXruGBBx7AXXfd1YrFYucVRbmjUqkIy7LYG2+8IVOS9gaWlpag67oclKh/Rx+EB1FFdz2HaLVasrXGYjHpMJpHUqkUWq2WbMm+7/Pr16+Lq1evDrquO8AYO0/7AO/lgHoqlbKz2Wzy+vXrEs2r1SoSiQQOHDgARVHEW2+9lbhy5crtlmWh3W5z2uiidDVNc83aim3bKJfLa06WKb2jZ480TEXH4qhKRGd/NFqrqiqJERlPOmEymQwWFhbU06dPfwbA+fHxcb5+n1liQKQTNIrF4ktbtmzByspKQGpQPB7HLbfcgr1796Knp4fV63XFsiyFCBBR3nQ6jVwuh1QqJc//V1ZWUK/XZfSjBhNxIZ0hqh2Q8wgMo+oTHZdR3RO/CMMQ9Xodtm3DdV1UKhVRrVZ30eHvjR48snUNALjtttv+euPGjRBC8CAIkE6nkUgkUCwWYZomCoUC8vm8ZIcEQM1mE7VaDUtLS5iZmcH8/Dzm5+dRqVSwsrIi+zv9PV08HYFFxZUoc6QeT2oSdSbHcWTGrF/TWR2lBWOMRZXp99wUHR4e5l//+tfDxx9//OXvfe97++v1epDP55XLly+jv78fiURC1ner1cLqEZPs6YTC0bGUIkyUOLpPSEyRRBbq59EDTyoHOl0iR0WPyohoUbsMgkAEQSC+8IUvhA899NC+zZs3v/ZuGMCj/zh48CAPggC33Xbbt3bs2AHLsuQJTq1Ww9WrVwFAtkIynsCKmF9Uy1uv6qxfiKLP0MZZFASjxlMZkMgSNZz4guM4dJIVfPazn+X33HPPsd9l/I0cEABgg4ODz+7evXuqUCgo9EGKLj1JKDVNU+p7dDFU01SXhAfR9CcgW+XsMprRNbvo39Nnor8jhJBaIGVOEATuQw89pB49evS/3HTTTU+NjY2pv+tmCr5uQVAMDw8rjLH25s2bx/r7+8EYCznnsn3RkRPR36hmQCdI0fE3etHRQSmqEAVBsGZblHCBsoXk+egSpOu6WFlZod0k4ft+UCqVgq985Sv6vffe+9revXv/9fDwsJx13i8PwMGDBzEyMoJ4PD7T3d0tRYhms4lGoyGPraM6PxlBpRCVu6Irc4QVUSWIjCHnRAGSWiAdj5EGsOoo4XleqKqq6OnpUQ8cOKDs27cPAwMDJ7Zs2fIIY8yNboS+bwdECIpHAmM8Hsfy8jIMw0ClUpGRI4EkFotJRI6uqURXZchRtBu4usUp+zmt4K1fsI5mVRAEwnGcEAA6OjqUj3zkI8q+ffuwc+dOq6+v7++7u7sfK5VKY9F7HN5rG/5dHaBpmhk9nCDBInr62mw212xlUvTX3wsUXZB0XReJREKeDUSFDHJMlAh5nids2w6FELxYLLK+vj5laGgI27dvt3fs2PGLrVu3/l1vb+/fMcamqZMdP35cvB/jf6cDAASqqiKZTKJcLstdQZKpoobTIlT0QINSOLoqG90MITGF+naUydHDtu0AgNLf36/s2rULe/bsmdu+ffv/7uvre3b79u0nyWgyfHBwkB09ejQYGRl53/dAvasDEonEXDweR6vVQkdHh0xty7KkyEHpuX5NNroYTRhArYyc2m635bICjdMEmkEQhK1Wi/f29iq333679/GPf/z7hw8ffrJQKJyhu8QIxMfGxvj4+PjvfbfYuzpgaWlJAEAmk7mwWv+cFhdo15+o7fopLrr3s/40iPg8ZQyVV5TNMcbgOE6QzWaVO++8M7zjjjue+tznPvdN0zTPRCN98OBBafShQ4c+kOHv6oBz584JANi9e/f5n//857Zt24kwDIXjOKzVakk+H70HgIxdf3BB/09TJYFno9GAoiiwbVt2DiEEGo2Gv23bNvXBBx88/6UvfemLiUTi1Be/+EUcOXJEOXLkCI4cORIyxj5wtH+fm6aYEAJPPvnkqz/96U8/Wq/XBWNMWVlZkZI1bYmRM6jOyRhaX6GMoNXa6K0w1Epd1xWO44j9+/fz+++//38eO3bsnzPGqnfeeac6Pj4eftC7Qt/Pg9/oxbGxMYUxJrZt2/bf9+zZw8Mw9F3XFclkcs3aS3Q9lgwj8KM7S+j4jPb2HMdBq9USjuOEtm37zWYzSCQS7P777+df+9rX/s3DDz98H2OsOjo6qkxMTPh/TOPfNQOEEPR6+gc/+MErL7744sDZs2eF7/shAG5ZFiNeHlVq15/+rpIjEf6mVsRqrXMhBDdNExs3bkRfXx/27du3dO+99/7L3t7eZwAoQojw/baxP/Rxwy7wG7lPMMZYrV6v38kY+3e5XO6Rc+fOqZcuXYJlWeFq9Fl0I0xRFBEEgVhdeGSMMa6qKjMMQ0kmk8jlcujq6kKxWAy2bNlyZceOHc/t3bv3+W3btp1mjC2RYhtthX/sB3s/d4yuavcfee655/7V6dOn/+nly5c7Z2Zm5GkQPeLxuDyWoqEpk8lcLRaL5zds2HCur6/v/wwMDFzctWvXNQAzjDE38lv8j53uvy8Rkplw9OhR3tnZeQbAg0KITadOnfrE66+//ieLi4u32ratCyEajLEgl8vNdnd3v14qlS7H4/FKT0/P1d7e3jOapjWip0LRW3QGBwcFoTv+Pzz+L1NsvBxy7qHaAAAAAElFTkSuQmCC" width="26" height="26" style="border-radius:5px;display:block">'
    so_icon   = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>'
    chev_left = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>'
    chev_right= '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>'

    page_js = """<script>(function(){
  var sun='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>';
  var moon='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
  function applyTheme(t){var r=document.documentElement;r.classList.add('theme-switching');r.setAttribute('data-theme',t);requestAnimationFrame(function(){requestAnimationFrame(function(){r.classList.remove('theme-switching');});});try{localStorage.setItem('mc_theme',t);}catch(e){}var btn=document.getElementById('themeToggle');if(btn)btn.innerHTML=t==='dark'?sun:moon;}
  var tb=document.getElementById('themeToggle');var cur=document.documentElement.getAttribute('data-theme')||'light';if(tb){tb.innerHTML=cur==='dark'?sun:moon;tb.addEventListener('click',function(){applyTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');});}
  var closed=false;try{closed=localStorage.getItem('sb_closed')==='1';}catch(e){}
  if(closed)document.body.classList.add('sb-closed');
  var tog=document.getElementById('sbToggle');if(tog){tog.addEventListener('click',function(){closed=!closed;document.body.classList.toggle('sb-closed',closed);try{localStorage.setItem('sb_closed',closed?'1':'0');}catch(e){}});}
})();</script>"""

    return HTMLResponse(f"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(title)} · MailChad</title>
<script>(function(){{try{{var t=localStorage.getItem('mc_theme')||'light';document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}}})();</script>
<style>{_CSS}</style>
</head>
<body>
<aside class="sidebar">
  <a class="sb-brand" href="/admin">
    <span class="mark">{mail_svg}</span>
    <span class="sb-wordmark">MailChad<span class="v">v3</span></span>
  </a>
  <nav class="sb-nav">{nav_html}</nav>
  <div class="sb-footer">
    <button class="sb-toggle" id="sbToggle" title="Toggle sidebar">
      <span class="sb-open-icon">{chev_left}</span>
      <span class="sb-close-icon" style="display:none">{chev_right}</span>
    </button>
  </div>
</aside>
<div class="main-wrap">
  <div class="top-bar">
    <span class="top-bar-title">{_html.escape(title)}</span>
    <span class="user-chip"><span class="avatar">{_html.escape(initials)}</span>{_html.escape(sub)}</span>
    <button class="icon-btn" id="themeToggle" title="Toggle theme" aria-label="Toggle theme"></button>
    <form method="post" action="/admin/auth/logout" style="margin:0"><button class="signout" type="submit" title="Sign out">{so_icon}</button></form>
  </div>
  <main class="content">
{body}
  </main>
</div>
{page_js}
{_HELP_JS}
<script>
/* Scroll position preservation across reloads */
(function(){{
  var k='ep_scroll_'+location.pathname;
  var y=sessionStorage.getItem(k);
  if(y){{window.scrollTo(0,parseInt(y));sessionStorage.removeItem(k);}}
}})();
function _reloadKeepScroll(){{
  sessionStorage.setItem('ep_scroll_'+location.pathname, window.scrollY);
  location.reload();
}}
</script>
<script>
(function(){{
  var b=document.body,tog=document.getElementById('sbToggle');
  function syncIcons(){{
    var c=b.classList.contains('sb-closed');
    var open=tog.querySelector('.sb-open-icon'),cls=tog.querySelector('.sb-close-icon');
    if(open)open.style.display=c?'none':'';
    if(cls)cls.style.display=c?'':'none';
  }}
  if(tog){{syncIcons();tog.addEventListener('click',syncIcons);}}
}})();
</script>
</body>
</html>""")


# SVG chart

_SERIES = [
    ("contacts",    "var(--series-1)"),
    ("campaigns",   "var(--series-2)"),
    ("dispatched",  "var(--series-3)"),
    ("suppression", "var(--series-4)"),
]

def _svg_chart(rows: list) -> str:
    W, H, PAD = 880, 240, 40
    if not rows:
        return (f"<svg width='{W}' height='{H}' style='width:100%;height:auto'>"
                f"<text x='50%' y='50%' text-anchor='middle' font-size='13' fill='var(--text-dim)'>"
                f"No data yet - reload in a moment.</text></svg>")
    n = len(rows)
    series_data = {k: [r[k] for r in rows] for k, _ in _SERIES}
    all_vals = [v for vals in series_data.values() for v in vals]
    y_max = max(max(all_vals), 1) * 1.08

    inner_w = W - PAD * 2 - 12
    inner_h = H - PAD - 18

    def px(i): return PAD + (i / max(n - 1, 1)) * inner_w
    def py(v): return 18 + inner_h - (v / y_max) * inner_h

    grid = ""
    for i in range(5):
        gy = 18 + i * inner_h / 4
        val = int(y_max * (1 - i / 4))
        grid += (f"<line x1='{PAD}' y1='{gy:.1f}' x2='{W-12}' y2='{gy:.1f}' "
                 f"stroke='var(--border)' stroke-width='1'/>")
        grid += (f"<text x='{PAD-8}' y='{gy+3:.1f}' text-anchor='end' font-size='10' "
                 f"font-family='var(--font-mono)' fill='var(--text-dim)'>{val}</text>")

    x_labels = ""
    if rows:
        x_labels += (f"<text x='{PAD}' y='{H-4}' font-size='10' fill='var(--text-dim)'>"
                     f"{rows[0]['ts'][11:16]}</text>")
        x_labels += (f"<text x='{W-12}' y='{H-4}' font-size='10' fill='var(--text-dim)' "
                     f"text-anchor='end'>{rows[-1]['ts'][11:16]}</text>")

    areas = ""
    lines = ""
    legend = ""
    for idx, (key, color) in enumerate(_SERIES):
        vals = series_data[key]
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
        area_d = (f"M{px(0):.1f},{H-PAD} " +
                  " ".join(f"L{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals)) +
                  f" L{px(n-1):.1f},{H-PAD} Z")
        areas += f"<path d='{area_d}' fill='{color}' opacity='0.07'/>"
        lines += (f"<polyline points='{pts}' fill='none' stroke='{color}' "
                  f"stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")
        ex, ey = px(n - 1), py(vals[-1])
        lines += f"<circle cx='{ex:.1f}' cy='{ey:.1f}' r='3' fill='{color}'/>"
        lx = PAD + idx * 130
        legend += f"<rect x='{lx}' y='6' width='10' height='10' fill='{color}' rx='2'/>"
        legend += f"<text x='{lx+14}' y='15' font-size='11' fill='var(--text-muted)'>{key}</text>"

    return (f"<svg viewBox='0 0 {W} {H}' style='width:100%;height:auto;display:block'>"
            f"{grid}{areas}{lines}{x_labels}{legend}</svg>")


def _help_btn(overlay_id: str, label: str = "? Format") -> str:
    return f'<button class="btn ghost sm" onclick="openHelp(\'{overlay_id}\')" title="Show format documentation">{label}</button>'


def _help_overlay(overlay_id: str, title: str, body_html: str) -> str:
    close_x = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>'
    return f"""<div class="help-overlay" id="{overlay_id}" onclick="if(event.target===this)closeHelp('{overlay_id}')">
<div class="help-card">
  <div class="modal-head"><h3>{title}</h3><button class="icon-btn" onclick="closeHelp('{overlay_id}')">{close_x}</button></div>
  <div class="modal-body" style="padding:18px 20px">{body_html}</div>
</div>
</div>"""


_HELP_JS = """<script>
function openHelp(id){document.getElementById(id).classList.add('open');}
function closeHelp(id){document.getElementById(id).classList.remove('open');}
document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.querySelectorAll('.help-overlay.open').forEach(function(el){el.classList.remove('open');});}});
</script>"""


def _stages_html(cid: int, stages: list, tmpl_opts: str) -> str:
    STATUS_COLOR = {"pending": "info", "dispatched": "ok", "failed": "crit", "paused": "warn"}
    rows_html = ""
    for s in stages:
        sc = STATUS_COLOR.get(s["status"], "")
        rows_html += (
            f'<div class="row" style="gap:12px;padding:10px 0;border-bottom:1px solid var(--border);align-items:center">'
            f'<span class="pill {sc}" style="flex-shrink:0"><span class="dot"></span>Stage {s["stage_number"]}</span>'
            f'<div style="flex:1;min-width:0">'
            f'<div class="cell-strong" style="font-size:13px">{_html.escape(s["template_name"])}</div>'
            f'<div class="dim" style="font-size:12px">{(s["scheduled_for"] or "-")[:16]}'
            + (f' · {_html.escape(s["note"])}' if s.get("note") else "") +
            f'{"  ·  Dispatched " + (s["dispatched_at"] or "")[:16] if s["status"] == "dispatched" else ""}'
            f'</div></div>'
            f'<div class="row-actions">'
            + (f'<form method="post" action="/admin/campaigns/{cid}/stages/{s["id"]}/delete" style="display:inline">'
               f'<button class="btn ghost sm" style="color:var(--danger-fg)" {"disabled" if s["status"]=="dispatched" else ""}>✕</button></form>'
               if s["status"] != "dispatched" else "") +
            f'</div></div>'
        )
    if not rows_html:
        rows_html = '<div class="dim" style="padding:12px 0">No stages yet - this is a single-shot campaign. Add stages below to turn it into a sequence.</div>'

    return f"""<div class="card pad mb" style="background:var(--surface-2)">
{rows_html}
</div>
<div class="form">
  <div class="section-title">Add a stage</div>
  <form method="post" action="/admin/campaigns/{cid}/stages/add">
    <div class="form-grid">
      <div class="field"><label>Template</label><select name="template_id" required>{tmpl_opts}</select></div>
      <div class="field"><label>Fire date &amp; time</label><input type="datetime-local" name="scheduled_for" required></div>
      <div class="field span-2"><label>Note <span class="hint">optional label</span></label><input type="text" name="note" placeholder="e.g. Day 3 follow-up"></div>
    </div>
    <div class="form-actions" style="margin-top:12px"><button class="btn primary" type="submit">Add stage</button></div>
  </form>
</div>"""


# routes

@router.get("/admin", response_class=HTMLResponse)
def overview(session=Depends(require_session)):
    with db.conn() as c:
        s = lambda q: c.execute(q).fetchone()[0]
        contacts    = s("SELECT count(*) FROM contacts")
        templates   = s("SELECT count(*) FROM templates")
        campaigns   = s("SELECT count(*) FROM campaigns")
        dispatched  = s("SELECT count(*) FROM dispatched_job WHERE status='dispatched'")
        suppression = s("SELECT count(*) FROM suppression_hashes")
        drift_crit  = s("SELECT count(*) FROM drift_report WHERE severity='CRITICAL' AND acknowledged_at IS NULL")
        drift_warn  = s("SELECT count(*) FROM drift_report WHERE severity='WARN' AND acknowledged_at IS NULL")

        # record snapshot (at most one per 5 min - compare to last entry)
        last = c.execute(
            "SELECT ts FROM metrics_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        import datetime
        now = datetime.datetime.utcnow()
        should_snap = True
        if last:
            try:
                last_ts = datetime.datetime.fromisoformat(last["ts"])
                if (now - last_ts).total_seconds() < 300:
                    should_snap = False
            except Exception:
                pass
        if should_snap:
            c.execute(
                "INSERT INTO metrics_snapshots (contacts,campaigns,dispatched,suppression) VALUES (?,?,?,?)",
                (contacts, campaigns, dispatched, suppression),
            )

        rows = c.execute(
            "SELECT ts,contacts,campaigns,dispatched,suppression FROM metrics_snapshots ORDER BY id DESC LIMIT 48"
        ).fetchall()
        rows = list(reversed(rows))

    chart = _svg_chart(rows)
    drift_crit_pill = f'<span class="pill crit"><span class="dot"></span>{drift_crit} critical</span>' if drift_crit else f'<span class="pill ok"><span class="dot"></span>0 critical</span>'
    drift_warn_pill = f'<span class="pill warn"><span class="dot"></span>{drift_warn} warnings</span>' if drift_warn else ''
    sync_ico = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>'
    dl_ico = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Vault health</div>
    <h1 class="page-title">Overview</h1>
    <p class="page-sub">Signed in as <b>{_html.escape(session.get('sub','?'))}</b></p>
  </div>
  <div class="head-actions">
    <form method="post" action="/sync/run" style="display:inline">
      <button class="btn">{sync_ico} Run sync now</button>
    </form>
    <a class="btn" href="/admin/backup/run" title="Run bin/v3 backup from the terminal">{dl_ico} Snapshot to backup</a>
  </div>
</div>

<div class="stat-strip mb">
  <div class="stat"><div class="k"><span class="led" style="background:var(--series-1)"></span>Contacts</div><div class="v">{contacts}</div></div>
  <div class="stat"><div class="k">Templates</div><div class="v">{templates}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--series-2)"></span>Campaigns</div><div class="v">{campaigns}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--series-3)"></span>Dispatched in-flight</div><div class="v {"accent" if dispatched else ""}">{dispatched}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--series-4)"></span>Suppressed</div><div class="v">{suppression}</div></div>
</div>

<div class="grid" style="grid-template-columns:1.7fr 1fr;align-items:start">
  <div class="card">
    <div class="card-head">
      <div><h3>Activity</h3><div class="dim" style="margin-top:2px">5-minute snapshots · last 4 hours</div></div>
      <div class="row gap-lg" style="font-size:12px;color:var(--text-muted)">
        <span class="row gap-sm"><span style="width:10px;height:10px;border-radius:3px;background:var(--series-1);display:inline-block"></span>contacts</span>
        <span class="row gap-sm"><span style="width:10px;height:10px;border-radius:3px;background:var(--series-2);display:inline-block"></span>campaigns</span>
        <span class="row gap-sm"><span style="width:10px;height:10px;border-radius:3px;background:var(--series-3);display:inline-block"></span>dispatched</span>
        <span class="row gap-sm"><span style="width:10px;height:10px;border-radius:3px;background:var(--series-4);display:inline-block"></span>suppression</span>
      </div>
    </div>
    <div class="card-body" style="padding:14px 16px 8px">{chart}</div>
  </div>

  <div class="stack" style="gap:16px">
    <div class="card pad">
      <div class="section-title" style="margin-bottom:14px">Drift</div>
      <div class="row" style="gap:10px">{drift_crit_pill}{drift_warn_pill}</div>
      <a href="/admin/drift" class="link" style="font-size:12.5px;display:inline-block;margin-top:12px">Review drift report -></a>
    </div>
    <div class="card pad">
      <div class="section-title between" style="display:flex;margin-bottom:14px">Sync status <span class="pill ok"><span class="dot"></span>healthy</span></div>
      <div class="stack" style="gap:11px;font-size:13px">
        <div class="row between"><span class="muted">WG actor</span><span class="mono cell-dim">{_html.escape(session.get('sub','?')[:20])}</span></div>
      </div>
    </div>
  </div>
</div>

{_config_section("System tuning", _setting_row("default_k_temp_ttl_s", "Pack key TTL (seconds)",
    help="How long the cloud holds K_temp for dispatch. 1h=3600, 24h=86400, 7d=604800."))}

<div id="setup-checklist" style="margin-top:24px"></div>
<div id="system-status-bar" style="margin-top:12px"></div>
<script>
(function(){{
  fetch('/admin/system/status').then(r=>r.json()).then(function(s){{
    var items=[], warns=[];
    if(!s.bootstrap_ok) warns.push('BOOTSTRAP_TOKEN is not set or is the default value - update your .env before going to production.');
    if(!s.handshake_done) items.push(['Handshake','Complete the cloud handshake (<code>bin/v3 init-handshake</code>) to enable sending.',false,null]);
    if(s.entity_count===0) items.push(['Entity','<a href="/admin/entities" class="link">Create your first sending entity</a> (domain + Resend key).',false,'/admin/entities']);
    else if(s.entity_with_key===0) items.push(['API key','<a href="/admin/entities" class="link">Set a Resend API key</a> on your entity.',false,'/admin/entities']);
    if(s.template_count===0) items.push(['Template','<a href="/admin/templates" class="link">Create or import a template</a>.',false,'/admin/templates']);
    if(s.contact_count===0) items.push(['Contacts','<a href="/admin/contacts" class="link">Import your contacts</a> (CSV or Excel).',false,'/admin/contacts']);
    if(s.using_dev_hmac) items.push(['HMAC secrets','Unsubscribe &amp; erasure links are using a dev fallback secret - set <code>unsub_secret</code> and <code>erasure_secret</code> in <a href="/admin/suppression" class="link">Suppression -> Config</a> before sending to real recipients. <b>Warning:</b> changing these after sending will break existing links.',false,'/admin/suppression']);
    if(s.k_temp_expiring) warns.push('Pack key (K_temp) is near expiry - it will auto-renew on next launch, but check the TTL setting.');
    if(items.length===0 && warns.length===0) return; // all good, hide checklist
    var el=document.getElementById('setup-checklist');
    var html='<div class="card pad" style="margin-bottom:16px"><div class="section-title" style="margin-bottom:14px">Setup checklist</div>';
    warns.forEach(function(w){{html+='<div class="callout warn" style="margin-bottom:10px">'+w+'</div>';}});
    items.forEach(function(it){{html+='<div class="row" style="gap:10px;padding:8px 0;border-bottom:1px solid var(--border)"><span style="color:var(--warn-fg);font-size:18px">○</span><div><b>'+it[0]+'</b><br><span class="dim" style="font-size:12.5px">'+it[1]+'</span></div></div>';}});
    html+='</div>';
    el.innerHTML=html;
    var sb=document.getElementById('system-status-bar');
    var kage=s.k_temp_age_s, kttl=s.k_temp_ttl_s;
    var kstr = kage==null?'Not minted':'Age '+Math.round(kage/3600)+'h / '+Math.round(kttl/3600)+'h TTL';
    var kpill = s.k_temp_expiring?'warn':(kage==null?'crit':'ok');
    sb.innerHTML='<div class="row" style="gap:12px;flex-wrap:wrap;font-size:12.5px;color:var(--text-muted);margin-bottom:4px">'+
      '<span><span class="pill '+(s.handshake_done?'ok':'crit')+'"><span class="dot"></span>Handshake '+(s.handshake_done?'done':'pending')+'</span></span>'+
      '<span><span class="pill '+kpill+'"><span class="dot"></span>K_temp: '+kstr+'</span></span>'+
      '<span><span class="pill '+(s.using_dev_hmac?'crit':'ok')+'"><span class="dot"></span>HMAC secrets: '+(s.using_dev_hmac?'⚠ dev fallback':'set')+'</span></span>'+
      '<span><span class="pill '+(s.webhook_24h>0?'ok':'')+'"><span class="dot"></span>Webhooks 24h: '+s.webhook_24h+'</span></span>'+
      '<span><span class="pill '+(s.inbox_pending>0?'warn':'ok')+'"><span class="dot"></span>Inbox pending: '+s.inbox_pending+'</span></span>'+
    '</div>';
  }}).catch(function(){{}});
}})();
</script>
"""
    return _layout("Overview", body, "/admin", session)


@router.get("/admin/backup/run", response_class=HTMLResponse)
def backup_run(session=Depends(require_session)):
    import subprocess, os
    backup_script = "/app/bin/_backup.py"
    backup_out    = "/var/lib/terminal/backups"
    pw            = os.environ.get("BACKUP_PASSPHRASE", "")
    if not pw:
        body = """
<div class="page-head"><div class="titles"><h1 class="page-title">Backup</h1></div></div>
<div class="callout crit" style="max-width:560px">
  <div class="ct-body"><b>BACKUP_PASSPHRASE not set.</b><br>
  Set it in your <code class="code">.env</code> file and restart the stack, or run manually:<br>
  <code class="code" style="display:block;margin-top:8px;white-space:pre">bin/v3 backup</code>
  </div>
</div>
<p style="margin-top:16px"><a class="link" href="/admin">← Overview</a></p>
"""
        return _layout("Backup", body, "/admin", session)

    try:
        os.makedirs(backup_out, exist_ok=True)
        env = {**os.environ, "BACKUP_OUT_DIR": backup_out, "BACKUP_PW": pw}
        result = subprocess.run(
            ["python3", backup_script, "--passphrase-env", "BACKUP_PW"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        output = _html.escape(result.stdout + result.stderr)
        ok = result.returncode == 0
        status_cls = "ok" if ok else "crit"
        status_msg = "Backup complete" if ok else "Backup failed"
    except Exception as e:
        output = _html.escape(str(e))
        status_cls, status_msg = "crit", "Backup error"

    body = f"""
<div class="page-head"><div class="titles"><h1 class="page-title">Backup</h1></div></div>
<div class="callout {status_cls}" style="margin-bottom:16px"><div class="ct-body"><b>{status_msg}</b></div></div>
<pre style="background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r);padding:14px;font-size:12px;overflow-x:auto;line-height:1.6">{output}</pre>
<p style="margin-top:16px"><a class="link" href="/admin">← Overview</a></p>
"""
    return _layout("Backup", body, "/admin", session)


@router.get("/admin/schedule", response_class=HTMLResponse)
def schedule_view(session=Depends(require_session)):
    from mailchad.terminal.routes_admin import get_schedule
    data = get_schedule()
    scheduled = data["scheduled"]
    active    = data["active"]
    paused    = data["paused"]
    recent    = data["recent"]

    cal_svg = _icon("schedule")
    back_svg = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>'

    STATUS_PILL = {"draft":"","tested":"info","scheduled":"accent","dispatched":"ok","paused":"warn","failed":"crit"}

    def _camp_row(r, show_queued=False):
        sp = STATUS_PILL.get(r["status"],"")
        scheduled_str = (r.get("scheduled_for") or "-")[:16]
        dispatched_str = (r.get("dispatched_at") or "-")[:16]
        queued_info = f'<span class="cell-dim">{r.get("queued_count",0):,} queued</span>' if show_queued else ""
        return (f'<tr>'
                f'<td class="cell-strong"><a class="link" href="/admin/campaigns/{r["id"]}/analytics">{_html.escape(r["name"])}</a></td>'
                f'<td><span class="pill {sp}"><span class="dot"></span>{r["status"]}</span></td>'
                f'<td class="cell-dim">{_html.escape(r.get("template_name") or "-")}</td>'
                f'<td class="cell-dim">{_html.escape(r.get("entity_name") or "-")}</td>'
                f'<td class="cell-dim">{scheduled_str}</td>'
                f'<td class="cell-dim">{dispatched_str}</td>'
                f'<td>{queued_info}</td>'
                f'<td class="actions"><div class="row-actions">'
                f'<a class="btn ghost sm" href="/admin/campaigns/{r["id"]}/analytics">Analytics</a>'
                f'</div></td></tr>')

    def _section(title, rows, show_queued=False, empty_msg="None"):
        if not rows:
            return f'<div class="section-title">{title}</div><p class="dim" style="margin-bottom:20px">{empty_msg}</p>'
        tbl = "".join(_camp_row(r, show_queued) for r in rows)
        return (f'<div class="section-title">{title} <span class="count">· {len(rows)}</span></div>'
                f'<div class="table-wrap mb"><table class="tbl">'
                f'<thead><tr><th>Campaign</th><th>Status</th><th>Template</th><th>Entity</th>'
                f'<th>Scheduled</th><th>Dispatched</th><th></th><th class="actions"></th></tr></thead>'
                f'<tbody>{tbl}</tbody></table></div>')

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Operations</div>
    <h1 class="page-title">Schedule</h1>
    <p class="page-sub">Upcoming, active, paused and recent campaigns. All times are local to the terminal.</p>
  </div>
  <div class="head-actions">
    <a class="btn primary" href="/admin/campaigns">New campaign</a>
  </div>
</div>
{_section("Upcoming - Scheduled", scheduled, empty_msg="No campaigns are scheduled.")}
{_section("Active - Dispatching", active, show_queued=True, empty_msg="No campaigns currently dispatching.")}
{_section("Paused", paused, empty_msg="No campaigns are paused.")}
{_section("Recent - Last 7 days", recent, empty_msg="No campaigns dispatched in the last 7 days.")}
"""
    return _layout("Schedule", body, "/admin/schedule", session)


@router.get("/admin/quicksend", response_class=HTMLResponse)
def quicksend_view(session=Depends(require_session)):
    with db.conn() as c:
        entities = c.execute(
            "SELECT id, name, from_email, from_name FROM entities WHERE resend_key_enc IS NOT NULL ORDER BY name"
        ).fetchall()
        templates = c.execute("SELECT id, name, subject, html_body FROM templates ORDER BY name").fetchall()

    entity_opts = "".join(f'<option value="{r["id"]}">{_html.escape(r["name"])} &lt;{_html.escape(r["from_email"] or "")}&gt;</option>' for r in entities)
    if not entity_opts:
        entity_opts = '<option value="">- no entities with key set -</option>'

    tmpl_opts = '<option value="">- none, use body below -</option>' + "".join(
        f'<option value="{r["id"]}" data-subject="{_html.escape(r["subject"])}" data-body="{_html.escape(r["html_body"][:2000])}">{_html.escape(r["name"])}</option>'
        for r in templates
    )

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Getting started</div>
    <h1 class="page-title">Quick send</h1>
    <p class="page-sub">Send a one-off email directly - no campaign, no contact tracking. Useful for testing setup or sending ad-hoc messages.</p>
  </div>
</div>
<div style="max-width:600px">
  <div class="form">
    <div class="field"><label>To <span class="hint">required</span></label><input type="email" id="qs-to" placeholder="recipient@example.com" required></div>
    <div class="field"><label>Entity (sender) <span class="hint">required</span></label><select id="qs-entity">{entity_opts}</select></div>
    <div class="field"><label>Template <span class="hint">optional - overrides subject and body</span></label><select id="qs-tmpl" onchange="qs_fillTemplate(this)">{tmpl_opts}</select></div>
    <div class="field"><label>Subject <span class="hint">required</span></label><input type="text" id="qs-subject" placeholder="Hello from MailChad"></div>
    <div class="field"><label>HTML body <span class="hint">required</span></label><textarea id="qs-body" class="mono" style="min-height:180px" placeholder="&lt;h1&gt;Hi!&lt;/h1&gt;&lt;p&gt;Your message here.&lt;/p&gt;"></textarea></div>
    <div class="form-actions">
      <button class="btn primary" id="qs-btn" onclick="qs_send()">Send now</button>
      <span id="qs-status" class="dim"></span>
    </div>
  </div>
  <div id="qs-result" style="margin-top:16px"></div>
</div>
<script>
function qs_fillTemplate(sel){{
  var opt=sel.options[sel.selectedIndex];
  if(!opt.value) return;
  document.getElementById('qs-subject').value=opt.dataset.subject||'';
  document.getElementById('qs-body').value=opt.dataset.body||'';
}}
async function qs_send(){{
  var to=document.getElementById('qs-to').value.trim();
  var entity=document.getElementById('qs-entity').value;
  var subject=document.getElementById('qs-subject').value.trim();
  var body=document.getElementById('qs-body').value.trim();
  var status=document.getElementById('qs-status');
  var res=document.getElementById('qs-result');
  if(!to||!subject||!body){{status.textContent='Fill in all required fields.';return;}}
  status.textContent='Sending…';
  document.getElementById('qs-btn').disabled=true;
  try{{
    var r=await fetch('/admin/quicksend',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{to_email:to,entity_id:entity?parseInt(entity):null,subject:subject,html_body:body}})}});
    var d=await r.json();
    if(d.sent){{
      res.innerHTML='<div class="callout ok"><div class="ct-body"><b>Sent!</b> Message ID: <code class="code">'+d.message_id+'</code></div></div>';
      status.textContent='';
    }} else {{
      res.innerHTML='<div class="callout crit"><div class="ct-body">'+JSON.stringify(d)+'</div></div>';
      status.textContent='';
    }}
  }}catch(e){{status.textContent='Error: '+e;}}
  document.getElementById('qs-btn').disabled=false;
}}
</script>
"""
    return _layout("Quick send", body, "/admin/quicksend", session)


@router.get("/admin/tags", response_class=HTMLResponse)
def tags_view(session=Depends(require_session)):
    from mailchad.terminal.routes_admin import list_tags_data
    tags = list_tags_data()
    with db.conn() as c:
        all_tags_for_opts = [t["tag"] for t in tags]
    tag_opts = "".join(f'<option value="{_html.escape(t)}">{_html.escape(t)}</option>' for t in all_tags_for_opts)

    def _tag_row(t):
        te = _html.escape(t["tag"])
        tj = te.replace("'", "\\'")
        cnt = t["count"]
        return (f'<tr class="tag-row" data-tag="{te}" data-count="{cnt}" onclick="rowClick(event,this)">'
                f'<td style="width:40px" onclick="event.stopPropagation()">'
                f'<input type="checkbox" class="tag-cb" value="{te}" onchange="selectionChanged()">'
                f'</td>'
                f'<td><span class="tag" style="font-size:13px;padding:3px 10px">{te}</span></td>'
                f'<td class="num" style="color:var(--text-muted)">{cnt}</td>'
                f'<td class="actions" style="width:44px" onclick="event.stopPropagation()">'
                f'<div style="position:relative">'
                f'<button class="btn ghost sm" style="font-size:16px;padding:2px 8px;line-height:1" '
                f"onclick=\"event.stopPropagation();toggleMenu('{tj}')\" title=\"Actions\">⋮</button>"
                f'<div class="tag-menu" id="menu-{te}" style="display:none;position:absolute;right:0;top:100%;z-index:200;'
                f'background:var(--surface);border:1px solid var(--border-2);border-radius:var(--r);'
                f'box-shadow:var(--shadow);min-width:180px;padding:4px 0">'
                f"<button class='tag-menu-item' onclick=\"closeMenus();openRenameModal('{tj}')\">✎ Rename</button>"
                f"<button class='tag-menu-item' onclick=\"closeMenus();openMergeModal('{tj}')\">⇄ Merge into…</button>"
                f"<button class='tag-menu-item' onclick=\"closeMenus();openAddModal('{tj}')\">＋ Add to contacts…</button>"
                f"<button class='tag-menu-item' onclick=\"closeMenus();loadContacts('{tj}')\">👁 View contacts</button>"
                f'<div style="height:1px;background:var(--border);margin:4px 0"></div>'
                f"<button class='tag-menu-item' style='color:var(--danger-fg)' onclick=\"closeMenus();openDeleteModal('{tj}',{cnt})\">✕ Remove from all contacts</button>"
                f'</div></div></td>'
                f'</tr>')

    rows_html = "".join(_tag_row(t) for t in tags) or '<tr><td colspan=3 class="empty" style="padding:40px">No tags yet - add tags to contacts via the Contacts page.</td></tr>'
    close_x = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>'

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Content</div>
    <h1 class="page-title">Tags</h1>
    <p class="page-sub">{len(tags)} tags · select a row to act on it, click a row to drill into its contacts.</p>
  </div>
</div>

<style>
.tag-row{{cursor:pointer;transition:background .08s}}
.tag-row:hover{{background:var(--surface-2)}}
.tag-row.selected{{background:var(--accent-soft)}}
.tag-toolbar{{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-sm) var(--r-sm) 0 0;border-bottom:none;flex-wrap:wrap}}
.tag-toolbar-count{{font-size:12.5px;color:var(--text-muted);margin-right:4px}}
th.sortable{{cursor:pointer;user-select:none}}
th.sortable:hover{{color:var(--text)}}
th.sortable .sort-icon{{margin-left:4px;opacity:.4;font-size:10px}}
th.sortable.asc .sort-icon::after{{content:'▲'}}
th.sortable.desc .sort-icon::after{{content:'▼'}}
th.sortable:not(.asc):not(.desc) .sort-icon::after{{content:'⇅'}}
.tag-menu-item{{display:block;width:100%;text-align:left;padding:7px 14px;font-size:13px;font-weight:500;background:none;border:none;cursor:pointer;color:var(--text);white-space:nowrap}}
.tag-menu-item:hover{{background:var(--surface-2)}}
</style>

<div class="grid" style="grid-template-columns:1fr 360px;align-items:start;gap:20px">
  <div>
    <!-- filter + create bar -->
    <div class="row between" style="margin-bottom:8px;gap:10px;flex-wrap:wrap">
      <div style="position:relative;flex:1;min-width:200px">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" style="position:absolute;left:10px;top:50%;transform:translateY(-50%);pointer-events:none;color:var(--text-dim)"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
        <input type="search" id="tag-filter" placeholder="Filter tags…" oninput="filterTags()" style="width:100%;padding:7px 10px 7px 32px;border:1px solid var(--border-2);border-radius:var(--r-sm);background:var(--surface);color:var(--text);font-size:13px">
      </div>
      <button class="btn primary" onclick="openCreatePanel()">+ Create tag</button>
    </div>

    <!-- action toolbar (shown when selection active) -->
    <div class="tag-toolbar" id="tag-toolbar" style="display:none">
      <span class="tag-toolbar-count" id="toolbar-count"></span>
      <button class="btn sm" id="tb-rename" onclick="toolbarRename()" disabled>Rename</button>
      <button class="btn sm" id="tb-merge"  onclick="toolbarMerge()"  disabled>Merge into…</button>
      <button class="btn sm" id="tb-add"    onclick="toolbarAdd()"    disabled>Add to contacts…</button>
      <button class="btn sm danger" onclick="toolbarDelete()">Delete</button>
      <button class="btn ghost sm" style="margin-left:auto" onclick="clearSelection()">Clear</button>
    </div>

    <!-- table -->
    <div class="table-wrap" id="tag-table-wrap" style="border-radius:0 0 var(--r-lg) var(--r-lg)">
      <table class="tbl" id="tag-table">
        <thead><tr>
          <th style="width:40px"><input type="checkbox" id="tag-all" onchange="toggleAll(this)"></th>
          <th class="sortable" data-col="tag" onclick="sortTable('tag')">Tag <span class="sort-icon"></span></th>
          <th class="sortable num" data-col="count" onclick="sortTable('count')">Contacts <span class="sort-icon"></span></th>
          <th style="width:44px"></th>
        </tr></thead>
        <tbody id="tag-tbody">{rows_html}</tbody>
      </table>
    </div>
    <div id="action-result" style="margin-top:12px"></div>
  </div>

  <div class="stack" style="gap:16px">
    <div class="card pad" id="create-panel" style="display:none">
      <div class="row between" style="margin-bottom:12px">
        <div class="section-title" style="margin:0">Create &amp; assign tag</div>
        <button class="btn ghost sm" onclick="document.getElementById('create-panel').style.display='none'">✕</button>
      </div>
      <p class="dim" style="font-size:13px;margin-bottom:12px">Add a new tag to all contacts that already have an existing tag, or to all contacts.</p>
      <div class="field"><label>New tag name <span class="req">required</span></label><input type="text" id="new-tag-name" placeholder="vip" class="mono"></div>
      <div class="field"><label>Scope to contacts with tag <span class="hint">optional</span></label>
        <select id="new-tag-filter"><option value="">- all contacts -</option>{tag_opts}</select>
      </div>
      <div class="form-actions"><button class="btn primary" onclick="createTag()">Create</button><button class="btn ghost" onclick="document.getElementById('create-panel').style.display='none'">Cancel</button></div>
      <div id="create-result" style="margin-top:10px"></div>
    </div>

    <div class="card pad" id="contacts-panel" style="display:none">
      <div class="row between" style="margin-bottom:10px">
        <div class="section-title" style="margin:0" id="contacts-panel-title">Contacts</div>
        <button class="btn ghost sm" onclick="document.getElementById('contacts-panel').style.display='none'">✕</button>
      </div>
      <!-- Add contacts to this tag -->
      <div style="margin-bottom:10px;display:flex;gap:6px">
        <input type="search" id="panel-search" placeholder="Search to add contacts…" style="flex:1;font-size:13px;padding:6px 10px;border:1px solid var(--border-2);border-radius:var(--r-sm);background:var(--surface);color:var(--text)" oninput="panelSearch()">
      </div>
      <div id="panel-search-results" style="margin-bottom:8px;max-height:140px;overflow-y:auto;font-size:12.5px"></div>
      <div class="divider" style="margin:8px 0"></div>
      <div class="dim" style="font-size:11.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin-bottom:6px">Has this tag</div>
      <div id="contacts-panel-body" class="stack" style="gap:4px;max-height:300px;overflow-y:auto;font-size:13px"></div>
    </div>
  </div>
</div>

<!-- Rename modal -->
<div class="modal-overlay" id="modal-rename" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Rename tag</h3><button class="icon-btn" onclick="document.getElementById('modal-rename').classList.remove('open')">{close_x}</button></div>
  <div class="modal-body">
    <p class="dim" style="margin-bottom:12px">Renames <span id="rename-old-display" class="tag"></span> across all contacts that have it.</p>
    <div class="field"><label>New name</label><input type="text" id="rename-new-val" class="mono" placeholder="new-tag-name"></div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-rename').classList.remove('open')">Cancel</button>
    <button class="btn primary" onclick="doRename()">Rename</button>
  </div>
</div></div>

<!-- Merge modal -->
<div class="modal-overlay" id="modal-merge" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Merge tag</h3><button class="icon-btn" onclick="document.getElementById('modal-merge').classList.remove('open')">{close_x}</button></div>
  <div class="modal-body">
    <p class="dim" style="margin-bottom:12px">Rename <span id="merge-old-display" class="tag"></span> to an existing tag - effectively merging the two.</p>
    <div class="field"><label>Merge into</label>
      <select id="merge-target-select">{tag_opts}</select>
    </div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-merge').classList.remove('open')">Cancel</button>
    <button class="btn primary" onclick="doMerge()">Merge</button>
  </div>
</div></div>

<!-- Add tag to contacts modal -->
<div class="modal-overlay" id="modal-add-tag" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Add tag to contacts</h3><button class="icon-btn" onclick="document.getElementById('modal-add-tag').classList.remove('open')">{close_x}</button></div>
  <div class="modal-body">
    <p class="dim" style="margin-bottom:12px">Add tag <span id="addtag-display" class="tag"></span> to all contacts that already have:</p>
    <div class="field"><label>Filter - contacts with this existing tag</label>
      <select id="addtag-filter"><option value="">- all contacts -</option>{tag_opts}</select>
    </div>
    <div id="addtag-count" class="dim" style="font-size:12.5px;margin-top:6px"></div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-add-tag').classList.remove('open')">Cancel</button>
    <button class="btn primary" onclick="doAddTag()">Add tag</button>
  </div>
</div></div>

<!-- Delete modal -->
<div class="modal-overlay" id="modal-delete" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Remove tag from all contacts</h3><button class="icon-btn" onclick="document.getElementById('modal-delete').classList.remove('open')">{close_x}</button></div>
  <div class="modal-body">
    <div class="callout crit" style="margin-bottom:12px">This removes <span id="delete-tag-display" class="tag" style="background:var(--crit-bg);color:var(--crit-fg)"></span> from <b id="delete-tag-count"></b> contacts. The contacts themselves are not deleted.</div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-delete').classList.remove('open')">Cancel</button>
    <button class="btn danger" onclick="doDelete()">Remove tag</button>
  </div>
</div></div>

<script>
var _renameOld='', _mergeOld='', _addTagName='', _deleteTag='';
var _sortCol='tag', _sortDir='asc';

/* Per-row kebab menu */
function toggleMenu(tag){{
  var m=document.getElementById('menu-'+tag);
  if(!m)return;
  var isOpen=m.style.display!=='none';
  closeMenus();
  if(!isOpen)m.style.display='';
}}
function closeMenus(){{
  document.querySelectorAll('.tag-menu').forEach(function(m){{m.style.display='none';}});
}}
document.addEventListener('click',function(e){{
  if(!e.target.closest('.tag-menu'))closeMenus();
}});

/* Selection */
function getSelected(){{
  return Array.from(document.querySelectorAll('.tag-cb:checked')).map(function(c){{return c.value;}});
}}
function selectionChanged(){{
  var sel=getSelected();
  var n=sel.length;
  var toolbar=document.getElementById('tag-toolbar');
  var wrap=document.getElementById('tag-table-wrap');
  toolbar.style.display=n?'flex':'none';
  wrap.style.borderRadius=n?'0':'var(--r-lg)';
  document.getElementById('toolbar-count').textContent=n+' selected';
  document.getElementById('tb-rename').disabled=n!==1;
  document.getElementById('tb-merge').disabled=n!==1;
  document.getElementById('tb-add').disabled=n!==1;
  document.querySelectorAll('.tag-row').forEach(function(r){{
    r.classList.toggle('selected',r.querySelector('.tag-cb').checked);
  }});
  var allCbs=document.querySelectorAll('.tag-cb');
  document.getElementById('tag-all').indeterminate=n>0&&n<allCbs.length;
  document.getElementById('tag-all').checked=n===allCbs.length;
}}
function toggleAll(cb){{
  document.querySelectorAll('.tag-cb:not([style*="display:none"])').forEach(function(c){{c.checked=cb.checked;}});
  selectionChanged();
}}
function clearSelection(){{
  document.querySelectorAll('.tag-cb').forEach(function(c){{c.checked=false;}});
  document.getElementById('tag-all').checked=false;
  selectionChanged();
}}
function rowClick(e,row){{
  if(e.target.type==='checkbox')return;
  var tag=row.dataset.tag;
  loadContacts(tag);
  document.querySelectorAll('.tag-row').forEach(function(r){{r.style.outline='none';}});
  row.style.outline='2px solid var(--accent)';
  row.style.outlineOffset='-2px';
}}

/* Filter */
function filterTags(){{
  var q=(document.getElementById('tag-filter').value||'').toLowerCase();
  document.querySelectorAll('.tag-row').forEach(function(r){{
    r.style.display=(!q||r.dataset.tag.toLowerCase().includes(q))?'':'none';
  }});
}}

/* Sort */
function sortTable(col){{
  var dir=(_sortCol===col&&_sortDir==='asc')?'desc':'asc';
  _sortCol=col; _sortDir=dir;
  document.querySelectorAll('th.sortable').forEach(function(th){{th.classList.remove('asc','desc');}});
  document.querySelector('th[data-col="'+col+'"]').classList.add(dir);
  var tbody=document.getElementById('tag-tbody');
  var rows=Array.from(tbody.querySelectorAll('.tag-row'));
  rows.sort(function(a,b){{
    var av=col==='count'?parseInt(a.dataset.count):a.dataset.tag;
    var bv=col==='count'?parseInt(b.dataset.count):b.dataset.tag;
    return dir==='asc'?(av>bv?1:av<bv?-1:0):(av<bv?1:av>bv?-1:0);
  }});
  rows.forEach(function(r){{tbody.appendChild(r);}});
}}

/* Toolbar -> modals */
function toolbarRename(){{var s=getSelected();if(s.length===1)openRenameModal(s[0]);}}
function toolbarMerge(){{var s=getSelected();if(s.length===1)openMergeModal(s[0]);}}
function toolbarAdd(){{var s=getSelected();if(s.length===1)openAddModal(s[0]);}}
async function toolbarDelete(){{
  var sel=getSelected();
  if(!sel.length)return;
  var total=sel.reduce(function(s,t){{
    var row=document.querySelector('.tag-row[data-tag="'+t+'"]');
    return s+(row?parseInt(row.dataset.count):0);
  }},0);
  _deleteTag=sel.join(',');
  document.getElementById('delete-tag-display').textContent=sel.join(', ');
  document.getElementById('delete-tag-count').textContent=total+' contacts';
  document.getElementById('modal-delete').classList.add('open');
}}

function setResult(html){{
  document.getElementById('action-result').innerHTML=html;
  setTimeout(function(){{_reloadKeepScroll();}},1400);
}}

/* Rename */
function openRenameModal(tag){{
  _renameOld=tag;
  document.getElementById('rename-old-display').textContent=tag;
  document.getElementById('rename-new-val').value=tag;
  document.getElementById('modal-rename').classList.add('open');
  setTimeout(function(){{document.getElementById('rename-new-val').select();}},80);
}}
async function doRename(){{
  var neu=document.getElementById('rename-new-val').value.trim();
  if(!neu||neu===_renameOld)return;
  document.getElementById('modal-rename').classList.remove('open');
  var r=await fetch('/admin/tags/rename',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{old_tag:_renameOld,new_tag:neu}})}});
  var d=await r.json();
  setResult('<div class="callout ok">Renamed <b>'+_renameOld+'</b> -> <b>'+neu+'</b> across '+d.updated+' contacts.</div>');
}}

/* Merge */
function openMergeModal(tag){{
  _mergeOld=tag;
  document.getElementById('merge-old-display').textContent=tag;
  document.getElementById('modal-merge').classList.add('open');
}}
async function doMerge(){{
  var target=document.getElementById('merge-target-select').value;
  if(!target||target===_mergeOld)return;
  document.getElementById('modal-merge').classList.remove('open');
  var r=await fetch('/admin/tags/rename',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{old_tag:_mergeOld,new_tag:target}})}});
  var d=await r.json();
  setResult('<div class="callout ok">Merged <b>'+_mergeOld+'</b> into <b>'+target+'</b> across '+d.updated+' contacts.</div>');
}}

/* Add tag to filtered contacts */
function openAddModal(tag){{
  _addTagName=tag;
  document.getElementById('addtag-display').textContent=tag;
  document.getElementById('addtag-count').textContent='';
  document.getElementById('modal-add-tag').classList.add('open');
}}
async function doAddTag(){{
  var filter=document.getElementById('addtag-filter').value;
  document.getElementById('modal-add-tag').classList.remove('open');
  var res=await fetch('/admin/tags/add-to-filtered',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{new_tag:_addTagName,filter_tag:filter||null}})}});
  if(res.ok){{var d=await res.json();setResult('<div class="callout ok">Added tag <b>'+_addTagName+'</b> to '+d.updated+' contacts.</div>');}}
  else{{setResult('<div class="callout crit">Failed.</div>');}}
}}

/* Delete (single or bulk) */
function openDeleteModal(tag,count){{_deleteTag=tag;document.getElementById('delete-tag-display').textContent=tag;document.getElementById('delete-tag-count').textContent=count;document.getElementById('modal-delete').classList.add('open');}}
async function doDelete(){{
  document.getElementById('modal-delete').classList.remove('open');
  var tags=_deleteTag.split(',').map(function(t){{return t.trim();}});
  var updated=0;
  for(var i=0;i<tags.length;i++){{
    var r=await fetch('/admin/tags/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tag:tags[i]}})}});
    var d=await r.json();
    updated+=d.updated||0;
  }}
  setResult('<div class="callout ok">Removed '+tags.length+' tag(s) from '+updated+' contacts.</div>');
}}

/* Create panel */
function openCreatePanel(){{
  document.getElementById('create-panel').style.display='';
  setTimeout(function(){{document.getElementById('new-tag-name').focus();}},50);
}}

/* Drill-down contacts */
var _panelTag='';
async function loadContacts(tag){{
  _panelTag=tag;
  document.getElementById('panel-search').value='';
  document.getElementById('panel-search-results').innerHTML='';
  var panel=document.getElementById('contacts-panel');
  var body=document.getElementById('contacts-panel-body');
  document.getElementById('contacts-panel-title').textContent='"'+tag+'" contacts';
  panel.style.display='';
  body.innerHTML='<span class="dim">Loading…</span>';
  await refreshTaggedContacts();
}}

async function refreshTaggedContacts(){{
  var body=document.getElementById('contacts-panel-body');
  var r=await fetch('/admin/contacts/json?limit=200&tag='+encodeURIComponent(_panelTag));
  var d=await r.json().catch(function(){{return null;}});
  if(!d||!d.contacts){{body.innerHTML='<span class="dim">Could not load.</span>';return;}}
  if(!d.contacts.length){{body.innerHTML='<span class="dim">No contacts with this tag.</span>';return;}}
  body.innerHTML=d.contacts.map(function(c){{
    var name=(c.name||'').substring(0,20);
    return '<div class="row between" style="padding:5px 0;border-bottom:1px solid var(--border);gap:8px">'+
      '<div style="min-width:0"><div class="cell-strong" style="font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+c.email+'</div>'+
      (name?'<div class="dim" style="font-size:11.5px">'+name+'</div>':'')+'</div>'+
      '<button class="btn ghost sm" style="color:var(--danger-fg);flex-shrink:0;font-size:11px;padding:2px 7px" onclick="removeTagFromContact('+c.id+',\''+c.email.replace(/'/g,"\\'")+"')>✕</button>"+
      '</div>';
  }}).join('')+(d.total>200?'<div class="dim" style="margin-top:6px;font-size:11.5px">+'+( d.total-200)+' more</div>':'');
}}

async function removeTagFromContact(id, email){{
  // Fetch current tags, strip this one, save
  var r=await fetch('/admin/contacts/json?limit=1&q='+encodeURIComponent(email));
  var d=await r.json().catch(function(){{return null;}});
  if(!d||!d.contacts.length)return;
  var c=d.contacts[0];
  var tags=(c.tags||'').split('|').map(function(t){{return t.trim();}}).filter(function(t){{return t&&t!==_panelTag;}});
  await fetch('/admin/contacts/'+id+'/edit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{tags:tags.join('|')||null}})}});
  refreshTaggedContacts();
}}

var _panelSearchTimer;
async function panelSearch(){{
  clearTimeout(_panelSearchTimer);
  var q=document.getElementById('panel-search').value.trim();
  var res=document.getElementById('panel-search-results');
  if(!q){{res.innerHTML='';return;}}
  _panelSearchTimer=setTimeout(async function(){{
    var r=await fetch('/admin/contacts/json?limit=20&q='+encodeURIComponent(q));
    var d=await r.json().catch(function(){{return null;}});
    if(!d||!d.contacts.length){{res.innerHTML='<div class="dim" style="padding:4px 0">No contacts found.</div>';return;}}
    res.innerHTML=d.contacts.map(function(c){{
      var hasTag=c.tags&&c.tags.split('|').map(function(t){{return t.trim();}}).indexOf(_panelTag)>-1;
      return '<div class="row between" style="padding:4px 0;border-bottom:1px solid var(--border);gap:8px">'+
        '<span style="font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">'+c.email+'</span>'+
        (hasTag
          ?'<span class="pill ok" style="font-size:10px;flex-shrink:0">has tag</span>'
          :'<button class="btn sm" style="flex-shrink:0;font-size:11px;padding:2px 8px" onclick="addTagToContact('+c.id+',\''+c.email.replace(/'/g,"\\'")+'\',\''+c.tags.replace(/'/g,"\\'")+"')>+ Add</button>"
        )+'</div>';
    }}).join('');
  }},250);
}}

async function addTagToContact(id, email, currentTags){{
  var tags=(currentTags||'').split('|').map(function(t){{return t.trim();}}).filter(Boolean);
  if(tags.indexOf(_panelTag)<0) tags.push(_panelTag);
  await fetch('/admin/contacts/'+id+'/edit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{tags:tags.join('|')}})}});
  document.getElementById('panel-search').value='';
  document.getElementById('panel-search-results').innerHTML='';
  refreshTaggedContacts();
}}

/* Create new tag */
async function createTag(){{
  var tagName=document.getElementById('new-tag-name').value.trim();
  var filter=document.getElementById('new-tag-filter').value;
  var res=document.getElementById('create-result');
  if(!tagName){{res.innerHTML='<div class="callout crit" style="margin-bottom:0">Enter a tag name.</div>';return;}}
  // Fetch contacts matching filter, then add tag to each via rename trick won't work.
  // Use add-to-filtered endpoint (we'll build it inline):
  var r=await fetch('/admin/tags/add-to-filtered',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{new_tag:tagName,filter_tag:filter||null}})}});
  if(r.ok){{
    var d=await r.json();
    res.innerHTML='<div class="callout ok">Added tag <b>'+tagName+'</b> to '+d.updated+' contacts.</div>';
    setTimeout(function(){{_reloadKeepScroll();}},1200);
  }} else {{
    res.innerHTML='<div class="callout crit">Failed: '+(await r.text())+'</div>';
  }}
}}
</script>
"""
    return _layout("Tags", body, "/admin/tags", session)


@router.post("/admin/tags/delete")
def tags_delete_post(session=Depends(require_session), tag: str = Form(...)):
    from mailchad.terminal.routes_admin import delete_tag, TagDeleteIn
    delete_tag(TagDeleteIn(tag=tag))
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/admin/tags/add-to-filtered")
async def tags_add_to_filtered(request: Request, session=Depends(require_session)):
    """Add a new tag to all contacts matching an optional filter tag."""
    from fastapi.responses import JSONResponse
    data = await request.json()
    new_tag = (data.get("new_tag") or "").strip()
    filter_tag = (data.get("filter_tag") or "").strip()
    if not new_tag:
        return JSONResponse({"error": "new_tag required"}, status_code=400)
    updated = 0
    with db.conn() as c:
        if filter_tag:
            rows = c.execute(
                "SELECT id, tags FROM contacts WHERE tags LIKE ?", (f"%{filter_tag}%",)
            ).fetchall()
        else:
            rows = c.execute("SELECT id, tags FROM contacts").fetchall()
        for r in rows:
            existing = [t.strip() for t in (r["tags"] or "").split("|") if t.strip()]
            if new_tag not in existing:
                existing.append(new_tag)
                c.execute("UPDATE contacts SET tags=? WHERE id=?", ("|".join(existing), r["id"]))
                updated += 1
        c.commit()
    return JSONResponse({"updated": updated})


@router.get("/admin/audit", response_class=HTMLResponse)
def audit_view(session=Depends(require_session), limit: int = 200):
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM audit_event ORDER BY occurred_at DESC LIMIT ?", (limit,)
        ).fetchall()]
    ACTION_PILL = {
        "terminal.started": "info", "terminal.stopped": "",
        "campaign.launched": "ok", "campaign.paused": "warn", "campaign.cloned": "info",
        "contact.deleted": "crit", "contacts.csv_import": "ok",
        "entity.created": "ok", "entity.deleted": "crit",
        "operator.created": "ok", "operator.deleted": "crit",
        "suppression.added": "warn",
    }
    def _row(r):
        action = r.get("action") or ""
        pill_cls = ACTION_PILL.get(action, "")
        detail_str = ""
        raw_details = dict(r).get("details_json") or dict(r).get("details") or ""
        if raw_details:
            try:
                import json as _j; d = _j.loads(raw_details)
                detail_str = " · ".join(f"{k}={v}" for k, v in list(d.items())[:3])
            except Exception:
                detail_str = str(raw_details)[:60]
        return (f'<tr><td class="cell-dim" style="white-space:nowrap">{(r.get("occurred_at") or "")[:16]}</td>'
                f'<td><span class="pill {pill_cls}" style="font-size:11px">{_html.escape(action)}</span></td>'
                f'<td class="cell-dim">{_html.escape(r.get("actor") or "")}</td>'
                f'<td class="cell-dim">{_html.escape(r.get("target") or "")}</td>'
                f'<td class="cell-dim" style="font-size:11.5px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_html.escape(detail_str)}</td>'
                f'</tr>')
    rows_html = "".join(_row(r) for r in rows) or '<tr><td colspan=5 class="empty" style="padding:32px">No audit events yet.</td></tr>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Operations</div>
    <h1 class="page-title">Audit log</h1>
    <p class="page-sub">Every operator action and system event. Last {limit} entries.</p>
  </div>
  <div class="head-actions">
    <a class="btn ghost sm" href="/admin/audit?limit=500">Last 500</a>
    <a class="btn ghost sm" href="/admin/audit?limit=50">Last 50</a>
  </div>
</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>When</th><th>Action</th><th>Actor</th><th>Target</th><th>Details</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""
    return _layout("Audit log", body, "/admin/audit", session)


@router.get("/admin/contacts/json")
def contacts_json(session=Depends(require_session), tag: str = "", q: str = "", limit: int = 100):
    """JSON endpoint for contacts - used by tags drill-down panel."""
    from fastapi.responses import JSONResponse
    search = q or tag
    with db.conn() as c:
        if search:
            term = f"%{search}%"
            rows = c.execute(
                "SELECT id, email, name, tags FROM contacts WHERE email LIKE ? OR name LIKE ? OR tags LIKE ? ORDER BY created_at DESC LIMIT ?",
                (term, term, term, limit),
            ).fetchall()
            total = c.execute(
                "SELECT count(*) FROM contacts WHERE email LIKE ? OR name LIKE ? OR tags LIKE ?",
                (term, term, term),
            ).fetchone()[0]
        else:
            rows = c.execute("SELECT id, email, name, tags FROM contacts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            total = c.execute("SELECT count(*) FROM contacts").fetchone()[0]
    return JSONResponse({"contacts": [dict(r) for r in rows], "total": total})


@router.get("/admin/contacts", response_class=HTMLResponse)
def contacts_view(session=Depends(require_session), tag: str = "", q: str = ""):
    search = q or tag  # q is new search param; tag kept for backward compat
    with db.conn() as c:
        if search:
            term = f"%{search}%"
            rows = c.execute(
                "SELECT id, email, name, tags, created_at FROM contacts "
                "WHERE email LIKE ? OR name LIKE ? OR tags LIKE ? ORDER BY created_at DESC LIMIT 300",
                (term, term, term),
            ).fetchall()
        else:
            rows = c.execute("SELECT id, email, name, tags, created_at FROM contacts ORDER BY created_at DESC LIMIT 300").fetchall()
    trash_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>'
    edit_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
    # Load campaign list for "add to campaign" modal
    with db.conn() as cc:
        campaigns_for_modal = cc.execute(
            "SELECT id, name FROM campaigns WHERE status NOT IN ('archived') ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    camp_opts = "".join(f'<option value="{c["id"]}">{_html.escape(c["name"])}</option>' for c in campaigns_for_modal)

    def _contact_row(r) -> str:
        tags_html = "".join(f'<span class="tag">{_html.escape(t)}</span>' for t in (r["tags"] or "").split("|") if t.strip())
        rid       = r["id"]
        name_esc  = _html.escape(r["name"] or "")
        tags_esc  = _html.escape(r["tags"] or "")
        email_esc = _html.escape(r["email"])
        edit_btn  = (f'<button class="btn ghost sm" onclick="editContact({rid},this)"'
                     f' title="Edit" data-name="{name_esc}" data-tags="{tags_esc}">'
                     f'{edit_svg}</button>')
        return (f'<tr>'
                f'<td style="width:36px"><input type="checkbox" class="bulk-cb" value="{rid}" onchange="bulkUpdate()"></td>'
                f'<td class="cell-dim mono">{rid}</td>'
                f'<td class="cell-strong">{email_esc}</td>'
                f'<td id="cn-{rid}">{name_esc}</td>'
                f'<td id="ct-{rid}"><span class="tags">{tags_html or "<span class=dim>-</span>"}</span></td>'
                f'<td class="cell-dim">{r["created_at"][:10]}</td>'
                f'<td class="actions"><div class="row-actions">'
                f'{edit_btn}'
                f'<form method="post" action="/admin/contacts/{rid}/delete" style="display:inline" onsubmit="return confirm(\'Delete contact?\')">'
                f'<button class="btn ghost sm" style="color:var(--danger-fg)">{trash_svg}</button></form>'
                f'</div></td></tr>')
    table_rows = "".join(_contact_row(r) for r in rows) or f'<tr><td colspan=7 class="empty" style="padding:32px">No contacts yet.</td></tr>'
    tag_filter_val = _html.escape(tag)
    total = len(rows)
    dl_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
    plus_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>'
    up_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Recipients</div>
    <h1 class="page-title">Contacts</h1>
    <p class="page-sub">All contacts are validated, lowercased, and hash-checked against suppression before insert.</p>
  </div>
  <div class="head-actions"><span class="dim">Total <b style="color:var(--text)">{total}</b></span></div>
</div>

<div class="grid cols-2 mb" style="align-items:start">
  <form class="form" method="post" action="/admin/contacts/create">
    <div class="section-title">Add a contact</div>
    <div class="form-grid">
      <div class="field span-2"><label for="ce">Email <span class="hint">required</span></label><input type="email" id="ce" name="email" required placeholder="jane@example.com"></div>
      <div class="field"><label for="cn">Name</label><input type="text" id="cn" name="name" placeholder="Jane Doe"></div>
      <div class="field"><label for="ct">Tags <span class="hint">pipe-separated</span></label><input type="text" id="ct" name="tags" class="mono" placeholder="beta|early-adopter"></div>
    </div>
    <div class="form-actions" style="margin-top:14px"><button class="btn primary" type="submit">{plus_svg} Add contact</button></div>
  </form>
  <div class="form">
    <div class="section-title">Bulk import</div>
    <p class="dim" style="margin-bottom:12px">Upload a CSV or Excel file. Columns: <code class="code">email</code> (required), <code class="code">name</code>, <code class="code">tags</code>, <code class="code">consent_source</code>, <code class="code">external_id</code>. No row limit - large files split automatically.</p>
    <div class="field"><label>CSV or Excel file</label><input type="file" id="csv-file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></div>
    <div class="field"><label>Batch tag <span class="hint">optional - applied to every imported contact</span></label><input type="text" id="csv-batch-tag" class="mono" placeholder="e.g. import-june-2026"></div>
    <div id="csv-info" class="dim" style="font-size:12.5px;margin-bottom:10px;display:none"></div>
    <div id="csv-progress" style="display:none;margin-bottom:10px">
      <div style="height:6px;background:var(--border);border-radius:99px;overflow:hidden"><div id="csv-bar" style="height:100%;background:var(--accent);width:0;transition:width .2s"></div></div>
      <div id="csv-status" class="dim" style="font-size:12px;margin-top:6px"></div>
    </div>
    <div id="csv-result" style="display:none"></div>
    <div class="form-actions"><button class="btn" id="csv-btn" disabled onclick="csvUpload()">{up_svg} Import CSV</button></div>
  </div>
<script>
(function(){{
  const CHUNK = 10000;
  const inp = document.getElementById('csv-file');
  inp.addEventListener('change', function() {{
    const info = document.getElementById('csv-info');
    document.getElementById('csv-result').style.display = 'none';
    document.getElementById('csv-progress').style.display = 'none';
    if (!inp.files[0]) {{ document.getElementById('csv-btn').disabled = true; info.style.display='none'; return; }}
    const isXlsx = inp.files[0].name.toLowerCase().endsWith('.xlsx');
    if (isXlsx) {{
      info.style.display = '';
      info.textContent = 'Excel file - will upload as single chunk (max 10,000 rows per sheet)';
      document.getElementById('csv-btn').disabled = false;
      return;
    }}
    const reader = new FileReader();
    reader.onload = function(e) {{
      const lines = e.target.result.split(/\\r?\\n/).filter(l => l.trim());
      const hasHeader = lines[0] && !lines[0].includes('@');
      const dataCount = hasHeader ? lines.length - 1 : lines.length;
      const chunks = Math.ceil(dataCount / CHUNK);
      info.style.display = '';
      info.textContent = dataCount.toLocaleString() + ' rows' + (chunks > 1 ? ' · will split into ' + chunks + ' chunks' : '');
      document.getElementById('csv-btn').disabled = false;
    }};
    reader.readAsText(inp.files[0]);
  }});
}})();

async function csvUpload() {{
  const inp = document.getElementById('csv-file');
  if (!inp.files[0]) return;
  const batchTag = (document.getElementById('csv-batch-tag')?.value || '').trim();
  const btn = document.getElementById('csv-btn');
  btn.disabled = true;
  const prog = document.getElementById('csv-progress');
  const bar = document.getElementById('csv-bar');
  const status = document.getElementById('csv-status');
  const result = document.getElementById('csv-result');
  prog.style.display = ''; result.style.display = 'none';

  const isXlsx = inp.files[0].name.toLowerCase().endsWith('.xlsx');
  let chunks;
  if (isXlsx) {{
    // Send xlsx whole - server handles it
    chunks = [{{isBlob: true, blob: inp.files[0], name: inp.files[0].name, type: inp.files[0].type}}];
  }} else {{
    const text = await inp.files[0].text();
    const allLines = text.split(/\\r?\\n/);
    const hasHeader = allLines[0] && !allLines[0].includes('@');
    const header = hasHeader ? allLines[0] : null;
    const dataLines = (hasHeader ? allLines.slice(1) : allLines).filter(l => l.trim());
    const CHUNK = 10000;
    chunks = [];
    for (let i = 0; i < dataLines.length; i += CHUNK) {{
      const rows = dataLines.slice(i, i + CHUNK);
      chunks.push(header ? [header, ...rows].join('\\n') : rows.join('\\n'));
    }}
  }}

  let imp = 0, sup = 0, inv = 0, errs = [];
  for (let i = 0; i < chunks.length; i++) {{
    bar.style.width = Math.round((i / chunks.length) * 100) + '%';
    status.textContent = 'Chunk ' + (i+1) + ' / ' + chunks.length + ' · imported so far: ' + imp.toLocaleString();
    const fd = new FormData();
    const chunk = chunks[i];
    if (chunk && chunk.isBlob) {{
      fd.append('file', chunk.blob, chunk.name);
    }} else {{
      fd.append('file', new Blob([chunk], {{type:'text/csv'}}), 'chunk.csv');
    }}
    if (batchTag) fd.append('batch_tag', batchTag);
    try {{
      const r = await fetch('/admin/contacts/csv/chunk', {{method:'POST', body:fd}});
      const d = await r.json();
      if (d.error) {{ errs.push('chunk ' + (i+1) + ': ' + d.error); continue; }}
      imp += d.imported || 0; sup += d.skipped_suppressed || 0; inv += d.skipped_invalid || 0;
      errs.push(...(d.errors || []).slice(0, 5));
    }} catch(e) {{ errs.push('chunk ' + (i+1) + ': network error'); }}
  }}
  bar.style.width = '100%';
  status.textContent = 'Done.';
  const errHtml = errs.length ? '<div class="dim" style="margin-top:6px;font-size:12px">' + errs.slice(0,10).map(e => '⚠ ' + e).join('<br>') + '</div>' : '';
  result.style.display = '';
  result.innerHTML = '<div class="callout ok" style="margin-top:10px"><div class="ct-body"><b>Imported ' + imp.toLocaleString() + ' contacts.</b><div class="dim" style="margin-top:4px">Skipped ' + sup + ' suppressed · ' + inv + ' invalid' + (errs.length ? ' · ' + errs.length + ' errors' : '') + '</div>' + errHtml + '</div></div>';
  btn.disabled = false;
  inp.value = '';
  document.getElementById('csv-info').textContent = '';
}}
</script>
</div>

<div class="row between mb" style="flex-wrap:wrap;gap:10px">
  <div class="row" style="gap:8px;align-items:center">
    <div class="section-title" style="margin:0">Contact list <span class="count">· {total:,}</span></div>
    <span id="bulk-bar" style="display:none;gap:8px;align-items:center" class="row">
      <span id="bulk-count" class="dim" style="font-size:12.5px"></span>
      <button class="btn sm" onclick="bulkExport()">Export</button>
      <button class="btn sm" onclick="openAddToCampaign()">Add to campaign</button>
      <button class="btn sm danger" onclick="bulkDelete()">Delete</button>
      <button class="btn ghost sm" onclick="bulkClear()">Clear</button>
    </span>
  </div>
  <div class="row" style="gap:8px;flex-wrap:wrap">
    <form method="get" action="/admin/contacts" style="display:flex;gap:6px;align-items:center">
      <input type="search" name="q" id="contact-q" value="{_html.escape(tag)}" placeholder="Search email, name, tag…" style="width:220px" oninput="this.form.submit()">
      {'<a class="btn ghost sm" href="/admin/contacts">✕</a>' if tag else ''}
    </form>
    <a class="btn ghost sm" href="/admin/contacts/export" id="export-all-btn">{dl_svg} Export CSV</a>
    {_help_btn("contacts-import-help")}
    {_help_btn("contacts-export-help")}
  </div>
</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th style="width:36px"><input type="checkbox" id="bulk-all" onchange="bulkToggleAll(this)"></th><th>ID</th><th>Email</th><th>Name</th><th>Tags</th><th>Created</th><th class="actions"></th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
<script>
function bulkUpdate(){{
  var cbs=document.querySelectorAll('.bulk-cb:checked');
  var bar=document.getElementById('bulk-bar');
  bar.style.display=cbs.length?'flex':'none';
  document.getElementById('bulk-count').textContent=cbs.length+' selected';
}}
function bulkToggleAll(cb){{
  document.querySelectorAll('.bulk-cb').forEach(function(c){{c.checked=cb.checked;}});
  bulkUpdate();
}}
function bulkClear(){{
  document.querySelectorAll('.bulk-cb').forEach(function(c){{c.checked=false;}});
  document.getElementById('bulk-all').checked=false;
  bulkUpdate();
}}
async function bulkDelete(){{
  var ids=Array.from(document.querySelectorAll('.bulk-cb:checked')).map(function(c){{return parseInt(c.value);}});
  if(!ids.length)return;
  if(!confirm('Delete '+ids.length+' contacts?'))return;
  await fetch('/admin/contacts/bulk-delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{ids:ids}})}});
  _reloadKeepScroll();
}}
function bulkExport(){{
  var ids=Array.from(document.querySelectorAll('.bulk-cb:checked')).map(function(c){{return c.value;}});
  window.location='/admin/contacts/export?ids='+ids.join(',');
}}
function openAddToCampaign(){{
  document.getElementById('modal-add-to-camp').classList.add('open');
}}
async function addToCampaign(){{
  var cid=document.getElementById('atc-camp-select').value;
  var ids=Array.from(document.querySelectorAll('.bulk-cb:checked')).map(function(c){{return parseInt(c.value);}});
  if(!cid||!ids.length)return;
  var r=await fetch('/admin/contacts/add-to-campaign',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{campaign_id:parseInt(cid),contact_ids:ids}})}});
  var d=await r.json();
  document.getElementById('modal-add-to-camp').classList.remove('open');
  alert('Added '+d.added+' contacts to campaign.');
  bulkClear();
}}
async function editContact(id,btn){{
  document.getElementById('edit-contact-id').value=id;
  document.getElementById('edit-contact-name').value=btn.dataset.name||'';
  document.getElementById('edit-contact-tags').value=btn.dataset.tags||'';
  document.getElementById('modal-edit-contact').classList.add('open');
}}
async function saveContact(){{
  var id=document.getElementById('edit-contact-id').value;
  var name=document.getElementById('edit-contact-name').value;
  var tags=document.getElementById('edit-contact-tags').value;
  await fetch('/admin/contacts/'+id+'/edit',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:name||null,tags:tags||null}})}});
  document.getElementById('modal-edit-contact').classList.remove('open');
  _reloadKeepScroll();
}}
</script>

<!-- Contact edit modal -->
<div class="modal-overlay" id="modal-edit-contact" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Edit contact</h3><button class="icon-btn" onclick="document.getElementById('modal-edit-contact').classList.remove('open')">✕</button></div>
  <div class="modal-body">
    <input type="hidden" id="edit-contact-id">
    <div class="field"><label>Name</label><input type="text" id="edit-contact-name" placeholder="Jane Doe"></div>
    <div class="field"><label>Tags <span class="hint">pipe-separated</span></label><input type="text" id="edit-contact-tags" class="mono" placeholder="beta|vip"></div>
  </div>
  <div class="modal-foot"><button class="btn ghost" onclick="document.getElementById('modal-edit-contact').classList.remove('open')">Cancel</button><button class="btn primary" onclick="saveContact()">Save</button></div>
</div></div>

<!-- Add to campaign modal -->
<div class="modal-overlay" id="modal-add-to-camp" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Add to campaign</h3><button class="icon-btn" onclick="document.getElementById('modal-add-to-camp').classList.remove('open')">✕</button></div>
  <div class="modal-body">
    <p class="dim" style="margin-bottom:12px">Add selected contacts as recipients to an existing campaign.</p>
    <div class="field"><label>Campaign</label><select id="atc-camp-select">{camp_opts}</select></div>
  </div>
  <div class="modal-foot"><button class="btn ghost" onclick="document.getElementById('modal-add-to-camp').classList.remove('open')">Cancel</button><button class="btn primary" onclick="addToCampaign()">Add</button></div>
</div></div>
{_help_overlay("contacts-import-help", "Contact import format",
    '<p class="dim" style="margin-bottom:12px">Upload a <b>CSV</b> or <b>Excel (.xlsx)</b> file. Header row is auto-detected.</p>'
    '<table class="field-table"><thead><tr><th>Column</th><th>Required</th><th>Notes</th></tr></thead><tbody>'
    '<tr><td><code class="code">email</code></td><td><span class="req">Required</span></td><td>Must contain @. Lowercased on import.</td></tr>'
    '<tr><td><code class="code">name</code></td><td>Optional</td><td>Full name for {{{{name}}}} / {{{{firstName}}}} variables.</td></tr>'
    '<tr><td><code class="code">tags</code></td><td>Optional</td><td>Pipe-separated: <code class="code">beta|vip|trial</code></td></tr>'
    '<tr><td><code class="code">consent_source</code></td><td>Optional</td><td>Defaults to <code class="code">csv_import</code>.</td></tr>'
    '<tr><td><code class="code">external_id</code></td><td>Optional</td><td>Your CRM ID for cross-reference.</td></tr>'
    '</tbody></table>'
    '<pre>email,name,tags,consent_source\njane@example.com,Jane Doe,beta|vip,signup-form\njohn@example.com,John,,</pre>'
    '<p class="dim" style="font-size:12px">Files &gt;10,000 rows are split automatically into chunks. Suppressed emails are skipped silently.</p>'
)}
{_help_overlay("contacts-export-help", "Contact export format",
    '<p class="dim" style="margin-bottom:12px">Exports a <b>CSV</b> with all visible columns. Select rows first to export a subset - no selection exports all.</p>'
    '<table class="field-table"><thead><tr><th>Column</th><th>Notes</th></tr></thead><tbody>'
    '<tr><td><code class="code">email</code></td><td>Lowercased address</td></tr>'
    '<tr><td><code class="code">name</code></td><td>Full name, may be blank</td></tr>'
    '<tr><td><code class="code">tags</code></td><td>Pipe-separated tag list</td></tr>'
    '<tr><td><code class="code">consent_source</code></td><td>Origin of consent</td></tr>'
    '<tr><td><code class="code">external_id</code></td><td>CRM reference, may be blank</td></tr>'
    '<tr><td><code class="code">created_at</code></td><td>ISO-8601 timestamp</td></tr>'
    '</tbody></table>'
    '<p class="dim" style="font-size:12px">The exported CSV can be re-imported to another instance as-is.</p>'
)}
"""
    return _layout("Contacts", body, "/admin/contacts", session)


@router.post("/admin/contacts/csv/chunk")
async def contacts_csv_chunk(session=Depends(require_session),
                              file: UploadFile = File(...),
                              batch_tag: str = Form("")):
    """JSON endpoint - receives one 10k-row chunk from the JS uploader."""
    from mailchad.terminal.routes_admin import csv_import
    from fastapi.responses import JSONResponse
    try:
        result = await csv_import(file, batch_tag=batch_tag)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/contacts/create")
def contacts_create(session=Depends(require_session),
                    email: str = Form(...), name: str = Form(""), tags: str = Form("")):
    from mailchad.terminal.routes_admin import create_contact, ContactIn
    try:
        create_contact(ContactIn(email=email, name=name or None, tags=tags or None))
    except Exception:
        pass
    return RedirectResponse("/admin/contacts", status_code=303)


@router.post("/admin/contacts/{contact_id}/edit")
async def contact_edit(contact_id: int, request: Request, session=Depends(require_session)):
    from fastapi.responses import JSONResponse
    from mailchad.terminal.routes_admin import edit_contact, ContactEditIn
    try:
        data = await request.json()
        result = edit_contact(contact_id, ContactEditIn(**data))
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/contacts/add-to-campaign")
async def contacts_add_to_campaign(request: Request, session=Depends(require_session)):
    from fastapi.responses import JSONResponse
    from mailchad.terminal.routes_admin import add_contacts_to_campaign, AddToCampaignIn
    try:
        data = await request.json()
        result = add_contacts_to_campaign(AddToCampaignIn(**data))
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/contacts/{cid}/delete")
def contacts_delete(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import delete_contact
    try:
        delete_contact(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/contacts", status_code=303)


@router.get("/admin/templates", response_class=HTMLResponse)
def templates_view(session=Depends(require_session), ok: str = "", error: str = ""):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM templates ORDER BY updated_at DESC").fetchall()
    eye_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
    def _tmpl_row(r) -> str:
        return (f'<tr>'
                f'<td style="width:36px"><input type="checkbox" class="tmpl-cb" value="{r["id"]}" onchange="tmplBulkUpdate()"></td>'
                f'<td class="cell-dim mono">{r["id"]}</td>'
                f'<td class="cell-strong"><a class="link" href="/admin/templates/{r["id"]}/preview">{_html.escape(r["name"])}</a></td>'
                f'<td class="cell-dim" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_html.escape(r["subject"])}</td>'
                f'<td class="cell-mono">{r["template_hash"][:8]}</td>'
                f'<td class="cell-dim">{r["updated_at"][:10]}</td>'
                f'<td class="actions"><div class="row-actions">'
                f'<a class="btn ghost sm" href="/admin/templates/{r["id"]}/preview">{eye_svg} Edit / Preview</a>'
                f'<form method="post" action="/admin/templates/{r["id"]}/delete" style="display:inline" onsubmit="return confirm(\'Delete this template? Campaigns using it will lose their template reference.\')">'
                f'<button class="btn ghost sm" style="color:var(--danger-fg)">Delete</button></form>'
                f'</div></td></tr>')
    table_rows = "".join(_tmpl_row(r) for r in rows) or '<tr><td colspan=7 class="empty" style="padding:32px">No templates yet.</td></tr>'
    VARS = [("{{firstName}}", "Jane"), ("{{name}}", "Jane Doe"), ("{{email}}", "jane@example.com"),
            ("{{unsubscribeUrl}}", "https://…/u/…"), ("{{erasureUrl}}", "https://…/e/…"), ("{{companyName}}", "MailChad")]
    vars_html = "".join(
        f'<div class="row between" style="font-size:12.5px;padding:5px 0;border-bottom:1px solid var(--border)">'
        f'<code class="code">{v[0]}</code><span class="dim" style="font-family:var(--font-mono)">{v[1]}</span></div>'
        for v in VARS)
    toast_html = ""
    if ok == "imported":
        toast_html = '<div class="callout ok" style="margin-bottom:16px">Template imported successfully.</div>'
    elif error:
        msgs = {"json_parse": "Could not parse JSON file.", "missing_from_name": "From name is required for HTML imports.",
                "missing_subject": "Could not determine subject - add a &lt;title&gt; tag or use JSON format.",
                "template name already exists": "A template with that name already exists."}
        toast_html = f'<div class="callout crit" style="margin-bottom:16px">{msgs.get(error, _html.escape(error))}</div>'
    upload_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>'
    dl_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
    body = f"""
{toast_html}
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Content</div>
    <h1 class="page-title">Templates</h1>
    <p class="page-sub">HTML email templates with variable substitution. A hash gate blocks launch if a template changes after being marked tested.</p>
  </div>
</div>

<div class="row between mb" style="align-items:center;flex-wrap:wrap;gap:10px">
  <div class="row" style="gap:8px;align-items:center">
    <div class="section-title" style="margin:0">All templates <span class="count">· {len(rows)}</span></div>
    <span id="tmpl-bulk-bar" style="display:none;gap:8px;align-items:center" class="row">
      <span id="tmpl-bulk-count" class="dim" style="font-size:12.5px"></span>
      <button class="btn sm" onclick="tmplExportSelected()">Export selected</button>
      <button class="btn ghost sm" onclick="tmplClearSelection()">Clear</button>
    </span>
  </div>
  <div class="row" style="gap:8px">
    <a class="btn ghost sm" href="/admin/templates/export" id="tmpl-export-all">{dl_svg} Export all</a>
    {_help_btn("tmpl-export-help")}
    {_help_btn("tmpl-import-help")}
  </div>
</div>
<div class="table-wrap mb">
  <table class="tbl">
    <thead><tr><th style="width:36px"><input type="checkbox" id="tmpl-all" onchange="document.querySelectorAll('.tmpl-cb').forEach(c=>{{c.checked=this.checked;}});tmplBulkUpdate()"></th><th>ID</th><th>Name</th><th>Subject</th><th>Hash</th><th>Updated</th><th class="actions"></th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
<script>
function tmplBulkUpdate(){{
  var cbs=document.querySelectorAll('.tmpl-cb:checked');
  var bar=document.getElementById('tmpl-bulk-bar');
  bar.style.display=cbs.length?'flex':'none';
  document.getElementById('tmpl-bulk-count').textContent=cbs.length+' selected';
}}
function tmplClearSelection(){{
  document.querySelectorAll('.tmpl-cb').forEach(function(c){{c.checked=false;}});
  document.getElementById('tmpl-all').checked=false;
  tmplBulkUpdate();
}}
function tmplExportSelected(){{
  var ids=Array.from(document.querySelectorAll('.tmpl-cb:checked')).map(function(c){{return c.value;}});
  window.location='/admin/templates/export?ids='+ids.join(',');
}}
</script>
{_help_overlay("tmpl-export-help", "Template export format",
    '<p class="dim" style="margin-bottom:12px">Exports a <b>JSON array</b>. Each object contains all template fields. Select rows to export a subset - no selection exports all.</p>'
    '<pre>[\n  {{\n    "name": "Welcome email",\n    "subject": "Hi {{{{firstName}}}}!",\n    "from_name": "The Team",\n    "html_body": "<h1>Hello</h1>",\n    "text_body": null,\n    "tracking_enabled": true\n  }}\n]</pre>'
    '<p class="dim" style="font-size:12px">Re-import via the Import card below. Duplicate names are skipped on import.</p>'
)}
{_help_overlay("tmpl-import-help", "Template import format",
    '<p class="dim" style="margin-bottom:12px">Upload a <b>.json</b> or <b>.html</b> file.</p>'
    '<table class="field-table"><thead><tr><th>Format</th><th>Notes</th></tr></thead><tbody>'
    '<tr><td><b>JSON (single object)</b></td><td>Must have <code class="code">name</code>, <code class="code">subject</code>, <code class="code">from_name</code>, <code class="code">html_body</code></td></tr>'
    '<tr><td><b>JSON (array)</b></td><td>Use <i>Import all</i> endpoint - bulk import multiple templates at once</td></tr>'
    '<tr><td><b>HTML file</b></td><td><code class="code">&lt;title&gt;</code> becomes subject, filename becomes name. Enter From name in the form.</td></tr>'
    '</tbody></table>'
    '<pre>{{\n  "name": "My template",\n  "subject": "Hello {{{{firstName}}}}",\n  "from_name": "Team",\n  "html_body": "<p>Hi!</p>"\n}}</pre>'
)}

<hr class="divider">

<div class="grid" style="grid-template-columns:1.5fr 1fr;align-items:start;gap:20px">
  <form class="form" method="post" action="/admin/templates/create">
    <div class="section-title">Create template</div>
    <div class="form-grid">
      <div class="field"><label for="tn">Name <span class="hint">required</span></label><input type="text" id="tn" name="name" required placeholder="Welcome - v2"></div>
      <div class="field"><label for="tf">From name <span class="hint">required</span></label><input type="text" id="tf" name="from_name" required placeholder="The MailChad team"></div>
      <div class="field span-2"><label for="ts">Subject <span class="hint">required</span></label><input type="text" id="ts" name="subject" required placeholder="Welcome aboard, {{{{firstName}}}} 👋"></div>
      <div class="field span-2">
        <div class="label-row"><label for="tb">HTML body</label><button type="button" class="btn ghost sm" onclick="insertBadge('tb')" title="Prepend a security badge to your email">✦ Insert security badge</button></div>
        <textarea id="tb" name="html_body" class="mono" required placeholder="&lt;h1&gt;Hi {{{{firstName}}}}&lt;/h1&gt;&#10;&lt;p&gt;Thanks for joining.&lt;/p&gt;&#10;&lt;a href=&quot;{{{{unsubscribeUrl}}}}&quot;&gt;Unsubscribe&lt;/a&gt;"></textarea>
      </div>
<script>
var BADGE_HTML = "<div style='display:inline-block;background:#ede9fe;color:#5b21b6;font-size:11px;font-weight:600;padding:3px 10px;border-radius:99px;letter-spacing:.04em;margin-bottom:20px'>✦ ENCRYPTED END-TO-END</div>\\n";
function insertBadge(id){{var el=document.getElementById(id);if(!el)return;if(el.value.indexOf('ENCRYPTED END-TO-END')===-1){{el.value=BADGE_HTML+el.value;}}if(typeof schedulePreview==='function')schedulePreview();}}
</script>
    </div>
    <div class="form-actions" style="margin-top:14px"><button class="btn primary" type="submit">Create template</button><span class="dim">Tracking enabled by default</span></div>
  </form>
  <div class="stack" style="gap:16px">
    <div class="card pad">
      <div class="section-title">{upload_svg} Import template</div>
      <p class="dim" style="margin-bottom:14px;font-size:13px">Upload a <code class="code">.json</code> or <code class="code">.html</code> file. JSON overrides all fields; HTML uses <code class="code">&lt;title&gt;</code> as subject.</p>
      <form method="post" action="/admin/templates/import" enctype="multipart/form-data">
        <div class="stack" style="gap:10px">
          <div class="field" style="margin:0"><label style="font-size:12.5px">File <span class="hint">.json or .html</span></label><input type="file" name="file" accept=".json,.html,text/html,application/json" required style="font-size:13px;padding:6px 0"></div>
          <div class="field" style="margin:0"><label style="font-size:12.5px">From name <span class="hint">required for HTML</span></label><input type="text" name="from_name" placeholder="The MailChad team" style="font-size:13px"></div>
          <div class="field" style="margin:0"><label style="font-size:12.5px">Override name <span class="hint">optional</span></label><input type="text" name="name" placeholder="Leave blank to use filename / JSON value" style="font-size:13px"></div>
        </div>
        <div class="form-actions" style="margin-top:12px"><button class="btn sm" type="submit">{upload_svg} Import</button></div>
      </form>
    </div>
    <div class="card pad">
      <div class="section-title">Available variables</div>
      <p class="dim" style="margin-bottom:12px;font-size:13px">Inserted at send time, per recipient.</p>
      <div class="stack">{vars_html}</div>
    </div>
  </div>
</div>
"""
    return _layout("Templates", body, "/admin/templates", session)




@router.get("/admin/templates/{tid}/preview", response_class=HTMLResponse)
def template_preview(tid: int, name: str = "Jane Doe", email: str = "jane@example.com",
                     contact_id: int = 0, session=Depends(require_session)):
    with db.conn() as c:
        tmpl = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        # Load real contact if requested
        if contact_id:
            rc = c.execute("SELECT email, name FROM contacts WHERE id=?", (contact_id,)).fetchone()
            if rc:
                email = rc["email"]
                name  = rc["name"] or email
        sample_contacts = c.execute(
            "SELECT id, email, name FROM contacts ORDER BY id DESC LIMIT 30"
        ).fetchall()
    if not tmpl:
        return RedirectResponse("/admin/templates", status_code=303)
    t = dict(tmpl)
    from mailchad.terminal.launch import _render, _inject_compliance
    contact = {"email": email, "name": name}
    subject, rendered_html, rendered_text = _render(t, contact)
    preview_html, _ = _inject_compliance(rendered_html, rendered_text,
                                          "https://example.com/unsub", "https://example.com/erasure",
                                          "promotional")
    escaped_html = _html.escape(preview_html)
    name_val = _html.escape(name)
    email_val = _html.escape(email)
    info_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
    back_svg = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>'
    VARS = [("{{firstName}}", "Jane"), ("{{name}}", "Jane Doe"), ("{{email}}", "jane@example.com"),
            ("{{unsubscribeUrl}}", "https://…/u/…"), ("{{erasureUrl}}", "https://…/e/…"), ("{{companyName}}", "MailChad")]
    vars_html = "".join(
        f'<div class="row between" style="font-size:11.5px;padding:4px 0;border-bottom:1px solid var(--border)">'
        f'<code class="code">{v[0]}</code><span class="dim">{v[1]}</span></div>'
        for v in VARS)
    html_body_escaped = _html.escape(t.get("html_body", ""))
    sample_contact_opts = "".join(
        f'<option value="{c["id"]}" data-email="{_html.escape(c["email"])}" data-name="{_html.escape(c["name"] or "")}">{_html.escape(c["email"])}</option>'
        for c in sample_contacts
    )
    body = f"""
<div class="row" style="gap:8px;margin-bottom:14px">
  <a class="link muted" href="/admin/templates" style="display:inline-flex;align-items:center;gap:5px;font-size:12.5px">{back_svg} Templates</a>
</div>
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Template</div>
    <h1 class="page-title">{_html.escape(t['name'])}</h1>
    <p class="page-sub">Live preview updates as you type. Edit the template body below and save - the preview refreshes instantly.</p>
  </div>
</div>

<div class="grid" style="grid-template-columns:minmax(260px,320px) 1fr;align-items:start;gap:20px">
  <div class="stack" style="gap:16px">
    <div class="card pad">
      <div class="section-title">Sample recipient</div>
      <div class="field"><label style="font-size:12px">From your contacts</label>
        <select onchange="var o=this.options[this.selectedIndex];if(o.value){{document.getElementById('pn').value=o.dataset.name||'';document.getElementById('pe').value=o.dataset.email||'';schedulePreview();}}">
          <option value="">- custom -</option>
          {sample_contact_opts}
        </select>
      </div>
      <div class="field"><label for="pn">Name</label><input type="text" id="pn" value="{name_val}" oninput="schedulePreview()"></div>
      <div class="field" style="margin-bottom:0"><label for="pe">Email</label><input type="email" id="pe" value="{email_val}" oninput="schedulePreview()"></div>
    </div>
    <div class="card pad">
      <div class="section-title">Headers</div>
      <div class="stack" style="gap:10px;font-size:12.5px">
        <div><div class="dim">Subject</div><div id="subj-display" class="cell-strong" style="margin-top:2px">{_html.escape(subject)}</div></div>
        <div><div class="dim">From</div><div class="cell-dim" style="margin-top:2px">{_html.escape(t.get('from_name',''))}</div></div>
        <div><div class="dim">Hash</div><div class="cell-mono" style="margin-top:2px">{t.get('template_hash','')[:8]}</div></div>
      </div>
    </div>
    <div class="card pad">
      <div class="section-title">Variables</div>
      <div class="stack">{vars_html}</div>
    </div>
    <div class="callout">
      {info_svg}
      <div class="ct-body">Unsubscribe &amp; erasure links are generated per recipient using configured HMAC secrets.</div>
    </div>
  </div>

  <div class="stack" style="gap:16px">
    <div class="card" style="overflow:hidden">
      <div class="card-head" style="display:flex;align-items:center;justify-content:space-between">
        <h3>Live preview</h3>
        <span id="preview-status" class="dim" style="font-size:12px"></span>
      </div>
      <div class="card-head" style="display:flex;align-items:center;justify-content:space-between">
        <h3>Live preview</h3>
        <div class="row" style="gap:6px">
          <button class="btn ghost sm" id="view-desktop" onclick="setViewport('desktop')" title="Desktop">🖥</button>
          <button class="btn ghost sm" id="view-mobile" onclick="setViewport('mobile')" title="Mobile (375px)">📱</button>
          <span id="preview-status" class="dim" style="font-size:12px"></span>
        </div>
      </div>
      <div style="padding:0;overflow:auto;background:var(--surface-2)"><iframe id="preview-frame" srcdoc="{escaped_html}" style="width:100%;min-height:480px;border:none;display:block;transition:width .2s"></iframe></div>
    </div>
    <div class="card pad">
      <div class="section-title">Edit template</div>
      <form method="post" action="/admin/templates/{tid}/save">
        <div class="form-grid">
          <div class="field"><label>Name</label><input type="text" name="name" value="{_html.escape(t['name'])}" required></div>
          <div class="field"><label>From name</label><input type="text" name="from_name" value="{_html.escape(t.get('from_name',''))}" required></div>
          <div class="field span-2"><label>Subject</label><input type="text" name="subject" value="{_html.escape(t.get('subject',''))}" required></div>
          <div class="field span-2">
            <div class="label-row"><label>HTML body</label><button type="button" class="btn ghost sm" onclick="insertBadge('html-body-input')" title="Prepend security badge">✦ Insert security badge</button></div>
            <textarea name="html_body" class="mono" id="html-body-input" style="min-height:220px" oninput="schedulePreview()">{html_body_escaped}</textarea>
          </div>
        </div>
        <div class="form-actions" style="margin-top:12px"><button class="btn primary" type="submit">Save changes</button></div>
      </form>
    </div>
  </div>
</div>
<script>
let _t;
function schedulePreview() {{
  clearTimeout(_t);
  _t = setTimeout(updatePreview, 600);
}}
async function updatePreview() {{
  const n = encodeURIComponent(document.getElementById('pn').value || 'Jane Doe');
  const e = encodeURIComponent(document.getElementById('pe').value || 'jane@example.com');
  const body = document.getElementById('html-body-input')?.value;
  const status = document.getElementById('preview-status');
  status.textContent = 'updating…';
  try {{
    let url = '/admin/templates/{tid}/preview/raw?name=' + n + '&email=' + e;
    let opts = {{}};
    if (body !== undefined) {{
      url = '/admin/templates/{tid}/preview/raw?name=' + n + '&email=' + e + '&body_override=1';
      opts = {{ method: 'POST', headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
               body: 'html_body=' + encodeURIComponent(body) }};
    }}
    const r = await fetch(url, opts);
    const html = await r.text();
    document.getElementById('preview-frame').srcdoc = html;
    status.textContent = 'updated';
    setTimeout(() => {{ status.textContent = ''; }}, 1500);
  }} catch(err) {{ status.textContent = 'error'; }}
}}
document.getElementById('pn').addEventListener('blur', updatePreview);
document.getElementById('pe').addEventListener('blur', updatePreview);
function setViewport(v){{
  var f=document.getElementById('preview-frame');
  if(v==='mobile'){{f.style.width='375px';f.style.margin='0 auto';}}
  else{{f.style.width='100%';f.style.margin='0';}}
  document.getElementById('view-desktop').style.opacity=v==='desktop'?1:0.5;
  document.getElementById('view-mobile').style.opacity=v==='mobile'?1:0.5;
}}
setViewport('desktop');
</script>
"""
    return _layout(f"Preview - {_html.escape(t['name'])}", body, "/admin/templates", session)


@router.get("/admin/templates/{tid}/preview/raw")
@router.post("/admin/templates/{tid}/preview/raw")
async def template_preview_raw(tid: int, name: str = "Jane Doe", email: str = "jane@example.com",
                                session=Depends(require_session),
                                html_body: str = Form(None)):
    from fastapi.responses import Response
    with db.conn() as c:
        tmpl = c.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
    if not tmpl:
        return Response("not found", status_code=404, media_type="text/plain")
    from mailchad.terminal.launch import _render, _inject_compliance
    t = dict(tmpl)
    if html_body is not None:
        t = {**t, "html_body": html_body}
    contact = {"email": email, "name": name}
    _, rendered_html, rendered_text = _render(t, contact)
    preview_html, _ = _inject_compliance(rendered_html, rendered_text,
                                          "https://example.com/unsub", "https://example.com/erasure",
                                          "promotional")
    return Response(preview_html, media_type="text/html")


@router.post("/admin/templates/{tid}/save")
def template_save(tid: int, session=Depends(require_session),
                  name: str = Form(...), subject: str = Form(...),
                  from_name: str = Form(...), html_body: str = Form(...)):
    from mailchad.terminal.routes_admin import update_template, TemplateIn
    try:
        update_template(tid, TemplateIn(name=name, subject=subject, from_name=from_name, html_body=html_body))
    except Exception:
        pass
    return RedirectResponse(f"/admin/templates/{tid}/preview", status_code=303)


@router.post("/admin/templates/{tid}/delete")
def template_delete(tid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import delete_template
    from fastapi.responses import JSONResponse
    try:
        delete_template(tid)
    except Exception:
        pass
    return RedirectResponse("/admin/templates", status_code=303)


@router.post("/admin/templates/create")
def templates_create(session=Depends(require_session),
                     name: str = Form(...), subject: str = Form(...),
                     from_name: str = Form(...), html_body: str = Form(...)):
    from mailchad.terminal.routes_admin import create_template, TemplateIn
    try:
        create_template(TemplateIn(name=name, subject=subject, from_name=from_name, html_body=html_body))
    except Exception:
        pass
    return RedirectResponse("/admin/templates", status_code=303)


@router.post("/admin/templates/import")
async def templates_import(session=Depends(require_session),
                           file: UploadFile = File(...),
                           from_name: str = Form(""),
                           name: str = Form("")):
    import json as _json, re as _re
    from mailchad.terminal.routes_admin import create_template, TemplateIn
    from fastapi import HTTPException

    raw = await file.read()
    fname = (file.filename or "imported").rsplit(".", 1)

    ext = fname[1].lower() if len(fname) == 2 else ""
    stem = fname[0]

    if ext == "json" or raw.lstrip()[:1] == b"{":
        try:
            data = _json.loads(raw)
        except Exception:
            return RedirectResponse("/admin/templates?error=json_parse", status_code=303)
        t = TemplateIn(
            name=data.get("name") or name or stem,
            subject=data.get("subject", ""),
            from_name=data.get("from_name") or from_name,
            html_body=data.get("html_body", ""),
            text_body=data.get("text_body"),
            tracking_enabled=data.get("tracking_enabled", True),
        )
    else:
        html_body = raw.decode("utf-8", errors="replace")
        title_m = _re.search(r"<title[^>]*>([^<]+)</title>", html_body, _re.I)
        subject = title_m.group(1).strip() if title_m else stem
        t = TemplateIn(
            name=name or stem,
            subject=subject,
            from_name=from_name,
            html_body=html_body,
        )

    if not t.from_name:
        return RedirectResponse("/admin/templates?error=missing_from_name", status_code=303)
    if not t.subject:
        return RedirectResponse("/admin/templates?error=missing_subject", status_code=303)

    try:
        create_template(t)
    except HTTPException as e:
        return RedirectResponse(f"/admin/templates?error={e.detail}", status_code=303)
    return RedirectResponse("/admin/templates?ok=imported", status_code=303)


@router.get("/admin/campaigns", response_class=HTMLResponse)
def campaigns_view(session=Depends(require_session)):
    with db.conn() as c:
        camps = c.execute(
            "SELECT c.id, c.name, c.kind, c.status, c.recipient_count, c.dispatched_at, c.scheduled_for, "
            "co.name AS entity_name, "
            "(SELECT count(*) FROM campaign_stages WHERE campaign_id=c.id) AS stage_count "
            "FROM campaigns c LEFT JOIN entities co ON co.id=c.entity_id ORDER BY c.created_at DESC"
        ).fetchall()
        templates = c.execute("SELECT id, name FROM templates ORDER BY name").fetchall()
        entities = c.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
    tmpl_opts = "".join(f"<option value='{t['id']}'>{_html.escape(t['name'])}</option>" for t in templates)
    co_opts = "<option value=''>- global (use settings key) -</option>" + "".join(
        f"<option value='{co['id']}'>{_html.escape(co['name'])}</option>" for co in entities
    )
    STATUS_PILL = {"draft": "", "tested": "info", "scheduled": "accent", "dispatched": "ok", "paused": "warn", "failed": "crit"}
    with db.conn() as cc:
        entities_for_edit = cc.execute("SELECT id, name FROM entities ORDER BY name").fetchall()
    ent_edit_opts = "<option value=''>- global -</option>" + "".join(
        f'<option value="{e["id"]}">{_html.escape(e["name"])}</option>' for e in entities_for_edit)

    def _camp_row(r) -> str:
        cid = r["id"]
        sp = STATUS_PILL.get(r["status"], "")
        st = r["status"]
        if st == "archived":
            return ""  # hide archived by default
        is_sent     = st in ("dispatched", "sent", "sending", "cancelled")
        launch_btn  = (f'<button class="btn sm primary" onclick="launchCampaign({cid},this,true)">Re-launch</button>'
                       if is_sent else
                       f'<button class="btn sm primary" onclick="launchCampaign({cid},this,false)">Launch</button>')
        pause_btn   = (f'<button class="btn ghost sm" title="Pause" onclick="campAction({cid},\'pause\')">⏸</button>') if st in ("dispatched","scheduled","tested") else ""
        resume_btn  = (f'<button class="btn ghost sm" title="Resume" onclick="campAction({cid},\'resume\')">▶</button>') if st == "paused" else ""
        clone_btn   = (f'<button class="btn ghost sm" title="Clone" onclick="campAction({cid},\'clone\')">⧉</button>')
        archive_btn = (f'<button class="btn ghost sm" title="Archive" style="color:var(--text-dim)" onclick="if(confirm(\'Archive?\'))campAction({cid},\'archive\')">⊡</button>') if st in ("dispatched","failed","cancelled") else ""
        name_esc = _html.escape(r["name"])
        rd = dict(r)
        ent_id = rd.get("entity_id") or ""
        rl = rd.get("rate_limit_per_min") or ""
        sc = rd.get("stage_count") or 0
        stage_badge = (f' <span class="pill info" style="font-size:10px;padding:1px 7px">'
                       f'📅 {sc} stages</span>') if sc else ""
        rid = r["id"]
        archive_menu_item = (f'<button class="more-menu-item" onclick="if(confirm(\'Archive?\'))campAction({cid},\'archive\')">Archive</button>'
                             if st in ("dispatched", "failed", "cancelled") else "")
        return (f'<tr><td class="cell-dim mono">{rid}</td>'
                f"<td class='cell-strong'><button class='btn ghost sm' style='padding:2px 6px;font-size:12px' onclick=\"editCampaign({rid},'{name_esc}','{ent_id}','{rl}')\">✎</button> {name_esc}{stage_badge}</td>"
                f'<td class="cell-dim">{r["kind"]}</td>'
                f'<td><span class="pill {sp}"><span class="dot"></span>{st}</span></td>'
                f'<td class="cell-dim">{_html.escape(r["entity_name"] or "-")}</td>'
                f'<td class="num cell-dim">{r["recipient_count"]}</td>'
                f'<td class="cell-dim">{(r["scheduled_for"] or "")[:16] or "-"}</td>'
                f'<td class="cell-dim">{(r["dispatched_at"] or "")[:16] or "-"}</td>'
                f'<td class="actions"><div class="row-actions">'
                f'{launch_btn}'
                f"<button class='btn sm' onclick=\"openTestSend({rid},'{name_esc}',{ent_id or 'null'})\">Test</button>"
                f'<button class="btn sm" onclick="campAction({cid},\'mark-tested\')">✓</button>'
                f'{pause_btn}{resume_btn}{clone_btn}'
                f'<div class="more-menu" style="position:relative;display:inline-block">'
                f'<button class="btn ghost sm" onclick="event.stopPropagation();toggleMenu(\'cm{rid}\')" title="More">⋯</button>'
                f'<div id="cm{rid}" class="more-menu-dd" style="display:none;position:absolute;right:0;top:100%;z-index:100;background:var(--surface);border:1px solid var(--border);border-radius:6px;box-shadow:var(--shadow-sm);min-width:150px;padding:4px 0">'
                f'<a class="more-menu-item" href="/admin/campaigns/{r["id"]}/recipients">Recipients</a>'
                f'<a class="more-menu-item" href="/admin/campaigns/{r["id"]}/analytics">Analytics</a>'
                f'{archive_menu_item}'
                f'</div></div>'
                f'</div></td></tr>')
    table_rows = "".join(_camp_row(r) for r in camps) or '<tr><td colspan=9 class="empty" style="padding:32px">No campaigns yet.</td></tr>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Lifecycle</div>
    <h1 class="page-title">Campaigns</h1>
    <p class="page-sub">Create -> add recipients -> mark tested -> launch. The template-hash gate blocks launch if the template changed after testing.</p>
  </div>
</div>

<div class="grid mb" style="grid-template-columns:1.4fr 1fr;align-items:start">
  <form class="form" method="post" action="/admin/campaigns/create">
    <div class="section-title">Create campaign</div>
    <div class="form-grid">
      <div class="field span-2"><label for="cn">Name <span class="hint">required</span></label><input type="text" id="cn" name="name" required placeholder="June feature launch"></div>
      <div class="field"><label for="ct">Template</label><select id="ct" name="template_id" required>{tmpl_opts}</select></div>
      <div class="field"><label for="ck">Kind</label><select id="ck" name="kind"><option value="promotional">promotional</option><option value="transactional">transactional</option></select></div>
      <div class="field"><label for="cc">Entity</label><select id="cc" name="entity_id">{co_opts}</select></div>
      <div class="field"><label for="cs">Schedule <span class="hint">optional</span></label><input type="datetime-local" id="cs" name="scheduled_for"></div>
      <div class="field"><label for="crl">Rate limit <span class="hint">emails / min - blank = unlimited</span></label><input type="number" id="crl" name="rate_limit_per_min" min="1" max="10000" placeholder="e.g. 60"></div>
      <div class="field"><label for="cbp">Bounce pause threshold <span class="hint">0.05 = 5% triggers halving</span></label><input type="number" id="cbp" name="bounce_pause_pct" min="0.01" max="1" step="0.01" value="0.10"></div>
      <div class="field span-2" style="border-top:1px solid var(--border);padding-top:14px;margin-top:2px">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
          <input type="checkbox" id="chs" name="human_send" value="1" onchange="toggleHuman(this.checked)" style="width:16px;height:16px;accent-color:var(--accent)">
          <span>Human-send emulator <span class="hint">randomise the delay between sends</span></span>
        </label>
        <div id="human-range" style="display:none;margin-top:12px">
          <div class="form-grid" style="grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px">
            <div class="field" style="margin:0"><label style="font-size:12.5px">Senders <span class="hint">parallel</span></label><input type="number" id="hs-count" name="human_send_count" value="1" min="1" max="50" oninput="calcTime()"></div>
            <div class="field" style="margin:0"><label style="font-size:12.5px">Min delay (s)</label><input type="number" id="hs-min" name="human_send_min_s" value="60" min="1" max="3600" oninput="calcTime()"></div>
            <div class="field" style="margin:0"><label style="font-size:12.5px">Max delay (s)</label><input type="number" id="hs-max" name="human_send_max_s" value="210" min="1" max="3600" oninput="calcTime()"></div>
          </div>
          <div class="field" style="margin:0"><label style="font-size:12.5px">Expected recipients <span class="hint">for estimate only</span></label><input type="number" id="hs-recip" value="100" min="1" oninput="calcTime()"></div>
          <div id="hs-estimate" class="callout" style="margin-top:10px;display:none"></div>
        </div>
      </div>
    </div>
    <div class="form-actions" style="margin-top:14px">
      <button class="btn primary" type="submit">Create campaign</button>
      <span class="dim">Leave schedule empty to keep as draft</span>
    </div>
  </form>
  <div class="card pad">
    <div class="section-title">Add recipients by tag</div>
    <p class="dim" style="margin-bottom:12px">Match contacts to a campaign by tag. Go to the campaign's Recipients page to add them.</p>
    <div class="callout" style="font-size:12.5px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:17px;height:17px;flex-shrink:0"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
      <div class="ct-body">Use the <b>Recipients</b> button on a campaign row to bulk-add contacts by tag.</div>
    </div>
  </div>
</div>

<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>ID</th><th>Name</th><th>Kind</th><th>Status</th><th>Entity</th><th class="num">Recipients</th><th>Scheduled</th><th>Dispatched</th><th class="actions"></th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
<script>
function toggleHuman(on) {{
  document.getElementById('human-range').style.display = on ? '' : 'none';
  if (on) calcTime();
}}
function calcTime() {{
  const n   = parseInt(document.getElementById('hs-count').value)  || 1;
  const mn  = parseInt(document.getElementById('hs-min').value)    || 60;
  const mx  = parseInt(document.getElementById('hs-max').value)    || 210;
  const rec = parseInt(document.getElementById('hs-recip').value)  || 100;
  const el  = document.getElementById('hs-estimate');
  if (mn > mx) {{ el.style.display=''; el.innerHTML='<span class="crit">Min must be ≤ max</span>'; return; }}
  const avgDelay = (mn + mx) / 2;
  const groups   = Math.ceil(rec / n);
  const secs     = groups * avgDelay;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const hstr = h ? h + 'h ' : '';
  const mstr = m + 'm';
  const rate = (n / avgDelay * 60).toFixed(1);
  el.style.display = '';
  el.innerHTML = '<b>~' + hstr + mstr + '</b> for ' + rec.toLocaleString() + ' recipients' +
    ' · ' + n + ' sender' + (n>1?'s':'') + ' · avg ' + avgDelay.toFixed(0) + 's gap' +
    ' · ~' + rate + ' emails/min effective';
}}
</script>

<!-- Campaign edit modal -->
<div class="modal-overlay" id="modal-edit-camp" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Edit campaign</h3><button class="icon-btn" onclick="document.getElementById('modal-edit-camp').classList.remove('open')">✕</button></div>
  <div class="modal-body">
    <input type="hidden" id="edit-camp-id">
    <div class="field"><label>Name</label><input type="text" id="edit-camp-name"></div>
    <div class="field"><label>Entity</label><select id="edit-camp-entity">{ent_edit_opts}</select></div>
    <div class="field"><label>Rate limit <span class="hint">emails/min, blank = unlimited</span></label><input type="number" id="edit-camp-rate" min="1" max="10000"></div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-edit-camp').classList.remove('open')">Cancel</button>
    <button class="btn primary" onclick="saveCampaign()">Save</button>
  </div>
</div></div>

<!-- Test send checkpoint modal -->
<div class="modal-overlay" id="modal-test-send" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal" style="max-width:520px">
  <div class="modal-head"><h3 id="ts-title">Test send</h3><button class="icon-btn" onclick="document.getElementById('modal-test-send').classList.remove('open')">✕</button></div>
  <div class="modal-body">
    <div class="field"><label>Send test to</label><input type="email" id="ts-email" placeholder="you@example.com"></div>
    <div id="ts-steps" style="margin-top:14px;display:none">
      <div class="stack" style="gap:8px" id="ts-step-list"></div>
    </div>
    <div id="ts-result" style="margin-top:12px"></div>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('modal-test-send').classList.remove('open')">Close</button>
    <button class="btn primary" id="ts-send-btn" onclick="runTestSend()">Run test send</button>
  </div>
</div></div>

<script>
var _editCampId=null;
function editCampaign(id,name,entId,rl){{
  _editCampId=id;
  document.getElementById('edit-camp-id').value=id;
  document.getElementById('edit-camp-name').value=name;
  document.getElementById('edit-camp-entity').value=entId||'';
  document.getElementById('edit-camp-rate').value=rl||'';
  document.getElementById('modal-edit-camp').classList.add('open');
}}
async function saveCampaign(){{
  var id=document.getElementById('edit-camp-id').value;
  var name=document.getElementById('edit-camp-name').value;
  var ent=document.getElementById('edit-camp-entity').value;
  var rl=document.getElementById('edit-camp-rate').value;
  await fetch('/admin/campaigns/'+id+'/edit',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{name:name||null,entity_id:ent?parseInt(ent):null,rate_limit_per_min:rl?parseInt(rl):null}})}});
  document.getElementById('modal-edit-camp').classList.remove('open');
  _reloadKeepScroll();
}}

var _testCampId=null, _testEntityId=null;

/* campaign ⋯ more-menu toggle - div id IS the passed id (e.g. "cm6") */
function toggleMenu(id){{
  var m=document.getElementById(id);
  if(!m)return;
  var isOpen=m.style.display!=='none';
  document.querySelectorAll('.more-menu-dd').forEach(function(d){{d.style.display='none';}});
  if(!isOpen)m.style.display='';
}}
document.addEventListener('click',function(e){{
  if(!e.target.closest('.more-menu'))
    document.querySelectorAll('.more-menu-dd').forEach(function(d){{d.style.display='none';}});
}});

/* verbs that only change campaign status - update row in place, no reload */
var _IN_PLACE_VERBS = {{'pause':1,'resume':1,'mark-tested':1,'archive':1}};
var _STATUS_LABELS  = {{'pause':'paused','resume':'tested','mark-tested':'tested','archive':'archived'}};
var _STATUS_PILL    = {{'draft':'','tested':'info','scheduled':'accent','dispatched':'ok','paused':'warn','failed':'crit','archived':''}};

function campAction(id, verb){{
  fetch('/admin/campaigns/'+id+'/'+verb,{{method:'POST',headers:{{'Content-Type':'application/json'}}}})
    .then(function(r){{
      if(!r.ok){{return r.text().then(function(t){{showToast(t||'Error '+r.status,'err');}});}}
      r.text().then(function(t){{
        var d=null;try{{d=JSON.parse(t);}}catch(e){{}}
        if(d&&d.detail){{showToast(d.detail,'err');return;}}

        if(_IN_PLACE_VERBS[verb]){{
          /* update only the status pill in the row - no scroll jump */
          var newStatus=_STATUS_LABELS[verb]||'';
          var pill=document.querySelector('tr[data-id="'+id+'"] .pill');
          if(!pill){{
            /* find by row containing a button with this campAction */
            var btns=document.querySelectorAll('[onclick*="campAction('+id+',"]');
            if(btns.length){{
              var row=btns[0].closest('tr');
              if(row)pill=row.querySelector('.pill');
            }}
          }}
          if(pill&&newStatus){{
            var cls=_STATUS_PILL[newStatus]||'';
            pill.className='pill'+(cls?' '+cls:'');
            pill.innerHTML='<span class="dot"></span>'+newStatus;
          }}
          showToast(verb.replace('-',' ')+' done','ok');
          /* full refresh after a moment so buttons update too */
          setTimeout(_reloadKeepScroll, 800);
        }} else {{
          _reloadKeepScroll();
        }}
      }});
    }}).catch(function(e){{showToast('Error: '+e,'err');}});
}}

function launchCampaign(id,btn,relaunch){{
  var label=relaunch?'Re-launch':'Launch';
  var endpoint=relaunch?'/admin/campaigns/'+id+'/relaunch':'/admin/campaigns/'+id+'/launch';
  if(!confirm(label+' campaign '+id+'?'))return;
  btn.disabled=true; btn.textContent='Sending…';
  fetch(endpoint,{{method:'POST',headers:{{'Content-Type':'application/json'}}}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.detail){{showToast(d.detail,'err');btn.disabled=false;btn.textContent=label;}}
      else{{showToast('Sent - '+d.dispatched+'/'+d.total_recipients+' dispatched','ok');setTimeout(function(){{_reloadKeepScroll();}},1200);}}
    }}).catch(function(e){{showToast('Error: '+e,'err');btn.disabled=false;btn.textContent=label;}});
}}

function showToast(msg,type){{
  var t=document.createElement('div');
  t.style.cssText='position:fixed;bottom:24px;right:24px;z-index:999;padding:12px 18px;border-radius:8px;font-size:13px;font-weight:500;box-shadow:0 4px 16px rgba(0,0,0,.2);max-width:360px';
  t.style.background=type==='ok'?'var(--ok-bg)':'var(--crit-bg)';
  t.style.color=type==='ok'?'var(--ok-fg)':'var(--crit-fg)';
  t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(function(){{t.remove();}},4000);
}}

(function(){{
  document.addEventListener('click',function(e){{
    if(!e.target.closest('.more-menu'))
      document.querySelectorAll('.more-menu-dd').forEach(function(d){{d.style.display='none';}});
  }});
}})();

function openTestSend(id,name,entityId){{
  _testEntityId=entityId;
  _testCampId=id;
  document.getElementById('ts-title').textContent='Test send - '+name;
  document.getElementById('ts-steps').style.display='none';
  document.getElementById('ts-step-list').innerHTML='';
  document.getElementById('ts-result').innerHTML='';
  document.getElementById('ts-send-btn').disabled=false;
  document.getElementById('modal-test-send').classList.add('open');
}}
function tsStep(label,state,note){{
  var icon={{pending:'⏳',ok:'✅',fail:'❌',skip:'⊡'}}[state]||'⏳';
  var color={{ok:'var(--ok-fg)',fail:'var(--crit-fg)',pending:'var(--text-muted)',skip:'var(--text-dim)'}}[state]||'var(--text-muted)';
  return '<div class="row" style="gap:10px;padding:6px 0;border-bottom:1px solid var(--border)"><span>'+icon+'</span><div style="flex:1"><b style="font-size:13px">'+label+'</b>'+(note?'<div class="dim" style="font-size:12px">'+note+'</div>':'')+'</div></div>';
}}
async function runTestSend(){{
  var email=document.getElementById('ts-email').value.trim();
  if(!email){{alert('Enter a recipient email.');return;}}
  var btn=document.getElementById('ts-send-btn');
  btn.disabled=true;
  document.getElementById('ts-steps').style.display='';
  var list=document.getElementById('ts-step-list');
  var res=document.getElementById('ts-result');
  list.innerHTML=''; res.innerHTML='';

  // Step 1: System checks
  list.innerHTML+=tsStep('Checking system chain…','pending');
  var chainRes=await fetch('/admin/campaigns/'+_testCampId+'/test-send-chain');
  var chain=await chainRes.json();
  list.innerHTML='';
  list.innerHTML+=tsStep('Template renders',chain.template_ok?'ok':'fail',chain.template_error||'');
  list.innerHTML+=tsStep('Encryption key (K_temp)',chain.k_temp_ok?'ok':'fail',chain.k_temp_error||'');
  list.innerHTML+=tsStep('Cloud reachable (bearer)',chain.cloud_ok?'ok':'fail',chain.cloud_error||'');
  list.innerHTML+=tsStep('Entity Resend key',chain.entity_ok?'ok':(chain.entity_error?'fail':'skip'),chain.entity_error||'using global key');

  var allOk=chain.template_ok&&chain.k_temp_ok&&chain.cloud_ok;
  if(!allOk){{
    res.innerHTML='<div class="callout crit" style="margin-top:12px">Chain check failed - fix the issues above before sending.</div>';
    btn.disabled=false; return;
  }}

  // Step 2: Quick send
  list.innerHTML+=tsStep('Sending via Resend…','pending');
  var sr=await fetch('/admin/quicksend',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{to_email:email,entity_id:null,subject:'[Test] Campaign '+_testCampId,
      html_body:'<p>This is a test send from MailChad campaign #'+_testCampId+'.</p>'}})}});
  var sd=await sr.json();
  // Update pending step
  list.innerHTML=list.innerHTML.replace('Sending via Resend…','Dispatched to Resend');
  list.innerHTML=list.innerHTML.replace('⏳','');
  list.innerHTML+=tsStep('Dispatched to Resend',sd.sent?'ok':'fail',sd.sent?'Message ID: '+sd.message_id:JSON.stringify(sd));

  if(sd.sent){{
    res.innerHTML='<div class="callout ok" style="margin-top:12px"><div class="ct-body"><b>Test sent to '+email+'</b><br><span class="dim">Message ID: '+sd.message_id+'<br>Mark as tested once confirmed.</span></div></div>';
    // Auto mark-tested offer
    res.innerHTML+='<form method="post" action="/admin/campaigns/'+_testCampId+'/mark-tested" style="margin-top:10px"><button class="btn sm primary">✓ Mark as tested</button></form>';
  }} else {{
    res.innerHTML='<div class="callout crit" style="margin-top:12px">Send failed: '+JSON.stringify(sd)+'</div>';
  }}
  btn.disabled=false;
}}
</script>
"""
    return _layout("Campaigns", body, "/admin/campaigns", session)


@router.post("/admin/campaigns/create")
def campaigns_create(session=Depends(require_session),
                     name: str = Form(...), template_id: int = Form(...),
                     kind: str = Form("promotional"), entity_id: str = Form(""),
                     scheduled_for: str = Form(""),
                     rate_limit_per_min: str = Form(""),
                     bounce_pause_pct: str = Form("0.10"),
                     human_send: str = Form(""),
                     human_send_min_s: str = Form("60"),
                     human_send_max_s: str = Form("210"),
                     human_send_count: str = Form("1")):
    from mailchad.terminal.routes_admin import create_campaign, CampaignIn
    try:
        cid = int(entity_id) if entity_id else None
        sf = scheduled_for.replace("T", " ") if scheduled_for else None
        rl = int(rate_limit_per_min) if rate_limit_per_min.strip() else None
        bp = float(bounce_pause_pct) if bounce_pause_pct.strip() else 0.10
        create_campaign(CampaignIn(name=name, template_id=template_id, kind=kind,
                                   entity_id=cid, scheduled_for=sf,
                                   rate_limit_per_min=rl, bounce_pause_pct=bp,
                                   human_send=bool(human_send),
                                   human_send_min_s=int(human_send_min_s or 60),
                                   human_send_max_s=int(human_send_max_s or 210),
                                   human_send_count=int(human_send_count or 1)))
    except Exception:
        pass
    return RedirectResponse("/admin/campaigns", status_code=303)


@router.post("/admin/campaigns/{cid}/edit")
async def campaign_edit(cid: int, request: Request, session=Depends(require_session)):
    from fastapi.responses import JSONResponse
    from mailchad.terminal.routes_admin import edit_campaign, CampaignEditIn
    try:
        data = await request.json()
        result = edit_campaign(cid, CampaignEditIn(**data))
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/campaigns/{cid}/archive")
def campaign_archive(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import archive_campaign
    try:
        archive_campaign(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/campaigns", status_code=303)


@router.get("/admin/campaigns/{cid}/recipients", response_class=HTMLResponse)
def campaign_recipients_view(cid: int, session=Depends(require_session)):
    with db.conn() as c:
        camp = c.execute("SELECT id, name, status FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not camp:
            return RedirectResponse("/admin/campaigns", status_code=303)
        count = c.execute(
            "SELECT count(*) AS n FROM campaign_recipients WHERE campaign_id=?", (cid,)
        ).fetchone()["n"]
        all_tags = []
        for row in c.execute("SELECT tags FROM contacts WHERE tags IS NOT NULL").fetchall():
            all_tags.extend((row["tags"] or "").split("|"))
        unique_tags = sorted(set(t.strip() for t in all_tags if t.strip()))
        stages = [dict(r) for r in c.execute(
            "SELECT cs.*, t.name AS template_name FROM campaign_stages cs "
            "JOIN templates t ON t.id=cs.template_id "
            "WHERE cs.campaign_id=? ORDER BY cs.stage_number",
            (cid,),
        ).fetchall()]
        tmpl_opts_stage = "".join(
            f'<option value="{r["id"]}">{_html.escape(r["name"])}</option>'
            for r in c.execute("SELECT id, name FROM templates ORDER BY name").fetchall()
        )

    tag_opts = "".join(f"<option>{_html.escape(t)}</option>" for t in unique_tags)
    back_svg = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>'
    STATUS_PILL = {"draft": "", "tested": "info", "scheduled": "accent", "dispatched": "ok"}
    sp = STATUS_PILL.get(camp["status"], "")
    body = f"""
<div class="row" style="gap:8px;margin-bottom:14px">
  <a class="link muted" href="/admin/campaigns" style="display:inline-flex;align-items:center;gap:5px;font-size:12.5px">{back_svg} Campaigns</a>
</div>
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Campaign #{cid} · <span class="pill {sp}" style="margin-left:4px"><span class="dot"></span>{camp["status"]}</span></div>
    <h1 class="page-title">{_html.escape(camp["name"])}</h1>
    <p class="page-sub">{count} recipient(s) added so far</p>
  </div>
</div>

<div class="grid cols-2 mb" style="align-items:start">
  <div class="form">
    <div class="section-title">Add by tag</div>
    <p class="dim" style="margin-bottom:12px">Adds all contacts whose tags contain the given token.</p>
    <form method="post" action="/admin/campaigns/{cid}/recipients/by-tag">
      <div class="field">
        <label for="tagIn">Tag</label>
        <div class="row gap-sm">
          <input type="text" id="tagIn" name="tag" list="tl1" required placeholder="e.g. beta" class="mono">
          <datalist id="tl1">{tag_opts}</datalist>
          <button class="btn primary" type="submit">Add matching</button>
        </div>
      </div>
    </form>
  </div>
  <div class="form">
    <div class="section-title">Preview match count</div>
    <p class="dim" style="margin-bottom:12px">See how many contacts a tag matches before adding.</p>
    <form method="get" action="/admin/contacts" target="_blank">
      <div class="field">
        <label for="tagIn2">Tag</label>
        <div class="row gap-sm">
          <input type="text" id="tagIn2" name="tag" list="tl2" placeholder="preview count" class="mono">
          <datalist id="tl2">{tag_opts}</datalist>
          <button class="btn" type="submit">View in contacts -></button>
        </div>
      </div>
    </form>
  </div>
</div>

<hr class="divider">
<div class="section-title" style="margin-bottom:12px">Email sequence (stages)
  <span class="count">· {len(stages)}</span>
  <span class="hint" style="font-weight:400;margin-left:6px">Each stage sends a different template to the same recipients at its scheduled time. The scheduler fires them automatically.</span>
</div>
{_stages_html(cid, stages, tmpl_opts_stage)}
"""
    return _layout(f"Recipients - {_html.escape(camp['name'])}", body, "/admin/campaigns", session)


@router.post("/admin/campaigns/{cid}/stages/add")
def campaign_add_stage(cid: int, session=Depends(require_session),
                        template_id: int = Form(...), scheduled_for: str = Form(...),
                        note: str = Form("")):
    from mailchad.terminal.routes_admin import add_stage, StageIn
    try:
        sf = scheduled_for.replace("T", " ") if scheduled_for else scheduled_for
        add_stage(cid, StageIn(template_id=template_id, scheduled_for=sf, note=note or None))
    except Exception:
        pass
    return RedirectResponse(f"/admin/campaigns/{cid}/recipients", status_code=303)


@router.post("/admin/campaigns/{cid}/stages/{stage_id}/delete")
def campaign_delete_stage(cid: int, stage_id: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import delete_stage
    try:
        delete_stage(cid, stage_id)
    except Exception:
        pass
    return RedirectResponse(f"/admin/campaigns/{cid}/recipients", status_code=303)


@router.post("/admin/campaigns/{cid}/recipients/by-tag")
def campaign_add_by_tag(cid: int, session=Depends(require_session), tag: str = Form(...)):
    from mailchad.terminal.routes_admin import add_recipients, AddRecipientsIn
    try:
        result = add_recipients(cid, AddRecipientsIn(by_tag=tag))
        added = result.get("added", 0)
    except Exception:
        added = 0
    return RedirectResponse(f"/admin/campaigns/{cid}/recipients?added={added}", status_code=303)


@router.post("/admin/campaigns/{cid}/mark-tested")
def campaigns_mark_tested(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import mark_tested
    try:
        mark_tested(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/campaigns", status_code=303)


def _human_send_estimate(camp: dict, queued: int) -> str:
    if not camp.get("human_send") or queued == 0:
        return ""
    n       = max(int(camp.get("human_send_count") or 1), 1)
    mn      = int(camp.get("human_send_min_s") or 60)
    mx      = int(camp.get("human_send_max_s") or 210)
    avg     = (mn + mx) / 2
    groups  = (queued + n - 1) // n
    secs    = groups * avg
    h, rem  = divmod(int(secs), 3600)
    m       = rem // 60
    hstr    = f"{h}h " if h else ""
    rate    = n / avg * 60
    return (f'<div class="callout" style="margin-bottom:16px">'
            f'<div class="ct-body"><b>~{hstr}{m}m remaining</b> for {queued:,} queued recipients'
            f' · {n} sender{"s" if n>1 else ""} · {mn}–{mx}s gap'
            f' · ~{rate:.1f} emails/min effective</div></div>')


@router.get("/admin/campaigns/{cid}/analytics", response_class=HTMLResponse)
def campaign_analytics_view(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import campaign_analytics_data
    try:
        data = campaign_analytics_data(cid)
    except Exception:
        return RedirectResponse("/admin/campaigns", status_code=303)

    camp = data["campaign"]
    s = data["summary"]
    rows = data["recipients"]

    def _stat(label: str, n: int, pct: str = "", color: str = "") -> str:
        style = f"color:{color}" if color else ""
        pct_str = f" <span class='dim'>({pct})</span>" if pct and pct != "-" else ""
        return (f"<div style='border:1px solid #eee;border-radius:6px;padding:.7em 1.2em;"
                f"text-align:center;min-width:90px'>"
                f"<div style='font-size:1.5em;font-weight:700;{style}'>{n}</div>"
                f"<div class='dim' style='font-size:.82em'>{label}{pct_str}</div></div>")

    summary_bar = (
        f"<div style='display:flex;gap:.8em;flex-wrap:wrap;margin:1em 0'>"
        f"{_stat('Sent', s['sent'])}"
        f"{_stat('Opened', s['opened'], s['opened_pct'], '#176')}"
        f"{_stat('Clicked', s['clicked'], s['clicked_pct'], '#4f8ef7')}"
        f"{_stat('Bounced', s['bounced'], s['bounced_pct'], '#960')}"
        f"{_stat('Complained', s['complained'], s['complained_pct'], '#a33')}"
        f"{_stat('Failed', s['failed'], color='#a33' if s['failed'] else '')}"
        f"</div>"
    )

    status_color = {"sent":"#eee","opened":"#cfd","clicked":"#d0e8ff",
                    "bounced":"#fec","complained":"#fcc","failed":"#fcc","queued":"#eee"}

    def _recipient_row(r: dict) -> str:
        bg = status_color.get(r["status"], "#eee")
        return (
            f"<tr>"
            f"<td>{_html.escape(r.get('email') or '-')}</td>"
            f"<td><span class='pill' style='background:{bg}'>{r['status']}</span></td>"
            f"<td class='dim'>{(r['sent_at'] or '')[:16]}</td>"
            f"<td class='dim'>{(r['opened_at'] or '')[:16]}</td>"
            f"<td><code class='dim'>{(r['message_id'] or '')[:16]}</code></td>"
            f"</tr>"
        )

    recipient_rows = "".join(_recipient_row(r) for r in rows) or \
        "<tr><td colspan=5 class='dim'>No recipients yet</td></tr>"

    has_failed = s["failed"] > 0 and camp["status"] in ("dispatched", "tested")
    retry_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>'
    back_svg = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>'
    STATUS_PILL = {"draft": "", "tested": "info", "scheduled": "accent", "dispatched": "ok"}
    sp = STATUS_PILL.get(camp["status"], "")
    retry_btn_html = (
        f'<form method="post" action="/admin/campaigns/{cid}/retry" style="display:inline">'
        f'<button class="btn danger">{retry_svg} Retry {s["failed"]} failed</button></form>'
    ) if has_failed else ""

    # Engagement funnel bar - opens are NOT tracked (bot noise); clicks are the signal
    bars = [("Sent", s["sent"], "var(--text-dim)"),
            ("Clicked", s["clicked"], "var(--accent)"), ("Bounced", s["bounced"], "var(--warn-fg)"),
            ("Failed", s["failed"], "var(--crit-fg)")]
    bar_html = ""
    total_sent = max(s["sent"], 1)
    for bname, bval, bcol in bars:
        pct = bval / total_sent * 100
        bar_html += (f'<div class="row" style="gap:12px"><div style="width:70px;font-size:12.5px" class="muted">{bname}</div>'
                     f'<div style="flex:1;height:16px;background:var(--surface-3);border-radius:4px;overflow:hidden">'
                     f'<div style="height:100%;width:{max(pct,0.4):.1f}%;background:{bcol};border-radius:4px"></div></div>'
                     f'<div style="width:100px;text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums">'
                     f'<b>{bval:,}</b> <span class="dim">{pct:.1f}%</span></div></div>')

    STATUS_R_PILL = {"sent": "", "opened": "ok", "clicked": "accent", "bounced": "warn", "complained": "crit", "failed": "crit", "queued": ""}
    def _r_row(r) -> str:
        rsp = STATUS_R_PILL.get(r["status"], "")
        return (f'<tr><td class="cell-strong">{_html.escape(r.get("email") or "-")}</td>'
                f'<td><span class="pill {rsp}"><span class="dot"></span>{r["status"]}</span></td>'
                f'<td class="cell-dim">{(r["sent_at"] or "")[:16]}</td>'
                f'<td class="cell-dim">{(r["clicked_at"] or "")[:16] or "-"}</td>'
                f'<td class="cell-mono">{(r["message_id"] or "")[:20]}</td></tr>')
    recip_rows = "".join(_r_row(r) for r in rows) or '<tr><td colspan=5 class="empty" style="padding:32px">No recipients yet.</td></tr>'

    # v3.22 batches card
    from mailchad.terminal import db as _db
    _now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _db.conn() as _c:
        batch_rows = [dict(b) for b in _c.execute(
            "SELECT * FROM campaign_batches WHERE campaign_id=? ORDER BY batch_no", (cid,)
        ).fetchall()]
        seed_list = [s["email"] for s in _c.execute(
            "SELECT email FROM seed_addresses WHERE active=1 ORDER BY id").fetchall()]
    batch_card = ""
    if batch_rows:
        B_PILL = {"pending":"", "scheduled":"info", "sending":"accent", "drained":"info",
                  "cooldown":"info", "approved":"ok", "done":"ok", "cancelled":"crit"}
        def _b_row(b):
            sp2 = B_PILL.get(b["status"], "")
            win = f'{(b["window_start"] or "")[:16]} -> {(b["window_end"] or "")[11:16]}' if b["window_start"] else "-"
            counts = (f'{b["sent"]} sent · {b["clicked"]} cl · '
                      f'{b["bounced"]} bn · {b["unsub"]} un')
            action = ""
            if b["status"] == "pending":
                prev = next((x for x in batch_rows if x["batch_no"] == b["batch_no"] - 1), None)
                ready = (prev is None) or (
                    prev["status"] in ("drained", "cooldown", "done")
                    and (not prev["approve_unlock_at"] or _now >= prev["approve_unlock_at"]))
                if ready:
                    action = (f'<button class="btn sm" onclick="approveBatch({cid},{b["batch_no"]})">'
                              f'Approve &amp; send</button>')
                elif prev and prev.get("approve_unlock_at"):
                    action = f'<span class="dim" style="font-size:11.5px">unlocks {prev["approve_unlock_at"][:16]} UTC</span>'
                else:
                    action = '<span class="dim" style="font-size:11.5px">waiting on prev batch</span>'
            return (f'<tr><td class="cell-strong">#{b["batch_no"]}</td>'
                    f'<td>{b["size"]}</td>'
                    f'<td><span class="pill {sp2}"><span class="dot"></span>{b["status"]}</span></td>'
                    f'<td class="cell-dim" style="font-size:11.5px">{win}</td>'
                    f'<td class="cell-dim" style="font-size:11.5px">{counts}</td>'
                    f'<td>{action}</td></tr>')
        brows = "".join(_b_row(b) for b in batch_rows)
        # seed placement logger (per drained/sending batch)
        seed_opts = "".join(f'<option value="{_html.escape(e)}">{_html.escape(e)}</option>' for e in seed_list)
        place_card = ""
        if seed_list:
            active_batch = next((b for b in batch_rows if b["status"] in ("sending","drained","cooldown")), None)
            if active_batch:
                place_card = f"""
  <div class="row" style="gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">
    <span class="dim" style="font-size:12px">Log seed placement (batch #{active_batch['batch_no']}):</span>
    <select id="seedSel" class="input sm" style="max-width:240px">{seed_opts}</select>
    <button class="btn ghost sm" onclick="logPlace({active_batch['id']},'inbox')">📥 Inbox</button>
    <button class="btn ghost sm" onclick="logPlace({active_batch['id']},'spam')">🚫 Spam</button>
    <button class="btn ghost sm" onclick="logPlace({active_batch['id']},'promotions')">🏷️ Promotions</button>
  </div>"""
        batch_card = f"""
<div class="card pad mb">
  <div class="section-title between" style="display:flex">Batches <span class="dim" style="font-weight:400">1000/batch · manual approve-next · {len(seed_list)} seed(s) salted</span></div>
  <div class="table-wrap" style="margin-top:8px">
    <table class="tbl">
      <thead><tr><th>Batch</th><th>Size</th><th>Status</th><th>Window (UTC)</th><th>Outcomes</th><th></th></tr></thead>
      <tbody>{brows}</tbody>
    </table>
  </div>{place_card}
</div>"""

    body = f"""
<div class="row" style="gap:8px;margin-bottom:14px">
  <a class="link muted" href="/admin/campaigns" style="display:inline-flex;align-items:center;gap:5px;font-size:12.5px">{back_svg} Campaigns</a>
</div>
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Campaign #{cid} · <span class="pill {sp}" style="margin-left:4px"><span class="dot"></span>{camp["status"]}</span></div>
    <h1 class="page-title">{_html.escape(camp["name"])}</h1>
    <p class="page-sub">{_html.escape(camp["kind"])}</p>
  </div>
  <div class="head-actions">{retry_btn_html}</div>
</div>

<div class="stat-strip mb">
  <div class="stat"><div class="k">Sent</div><div class="v">{s["sent"]:,}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--accent)"></span>Clicked</div><div class="v accent">{s["clicked"]}</div><div class="sub">{s["clicked_pct"]}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--warn-fg)"></span>Bounced</div><div class="v warn">{s["bounced"]}</div><div class="sub">{s["bounced_pct"]}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--crit-fg)"></span>Complained</div><div class="v crit">{s["complained"]}</div><div class="sub">{s["complained_pct"]}</div></div>
  <div class="stat"><div class="k"><span class="led" style="background:var(--crit-fg)"></span>Failed</div><div class="v{"" if not s["failed"] else " crit"}">{s["failed"]}</div></div>
  <div class="stat"><div class="k">Rate limit</div><div class="v" style="font-size:1em">{"human ×" + str(camp.get("human_send_count") or 1) if camp.get("human_send") else (str(camp.get("rate_limit_per_min") or "-") + ("/min" if camp.get("rate_limit_per_min") else ""))}</div></div>
  <div class="stat"><div class="k">{"Send delay" if camp.get("human_send") else "Bounce threshold"}</div><div class="v" style="font-size:1em">{f'{camp.get("human_send_min_s",60)}–{camp.get("human_send_max_s",210)}s' if camp.get("human_send") else f'{int((camp.get("bounce_pause_pct") or 0.10) * 100)}%'}</div></div>
</div>
{_human_send_estimate(camp, s["queued"] if "queued" in s else 0)}

<div class="card pad mb">
  <div class="section-title between" style="display:flex">Engagement funnel <span class="dim" style="font-weight:400">relative to sent</span></div>
  <div class="stack" style="gap:10px;margin-top:8px">{bar_html}</div>
</div>
{batch_card}
<div class="row between mb" style="flex-wrap:wrap;gap:12px">
  <div class="section-title" style="margin:0">Per-recipient <span class="count">· {len(rows)}</span></div>
  <span id="live-status" class="dim" style="font-size:12px"></span>
</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>Email</th><th>Status</th><th>Sent</th><th>Clicked</th><th>Message ID</th></tr></thead>
    <tbody id="recip-tbody">{recip_rows}</tbody>
  </table>
</div>
<script>
(function(){{
  var STATUS_COLORS={{'sent':'','opened':'ok','clicked':'accent','bounced':'warn','complained':'crit','failed':'crit','queued':''}};
  var queued={s["queued"]};
  if(queued===0)return; // nothing to poll
  var interval=setInterval(function(){{
    fetch('/admin/campaigns/{cid}/analytics/data')
      .then(function(r){{return r.json();}})
      .then(function(d){{
        var s=d.summary||{{}};
        document.getElementById('live-status').textContent='Live · sent '+s.sent+' / queued '+s.queued;
        if(s.queued===0){{clearInterval(interval);document.getElementById('live-status').textContent='✓ Dispatch complete';setTimeout(function(){{_reloadKeepScroll();}},1500);}}
      }}).catch(function(){{}});
  }},5000);
  document.getElementById('live-status').textContent='Dispatching… polling every 5s';
}})();
function approveBatch(cid, n){{
  if(!confirm('Release batch #'+n+' now? It schedules into the next send window.'))return;
  fetch('/admin/campaigns/'+cid+'/batches/'+n+'/approve',{{method:'POST'}})
    .then(function(r){{return r.json().then(function(j){{return {{ok:r.ok,j:j}};}});}})
    .then(function(x){{ if(!x.ok){{alert(x.j.detail||'approve failed');return;}} _reloadKeepScroll(); }})
    .catch(function(e){{alert('approve failed: '+e);}});
}}
function logPlace(batchId, placement){{
  var sel=document.getElementById('seedSel'); if(!sel||!sel.value)return;
  fetch('/admin/batches/'+batchId+'/placement',{{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{seed_email:sel.value, placement:placement}})}})
    .then(function(r){{ if(r.ok){{var ls=document.getElementById('live-status'); if(ls)ls.textContent='✓ logged '+sel.value+' -> '+placement;}} }})
    .catch(function(e){{alert('log failed: '+e);}});
}}
</script>
"""
    return _layout(f"Analytics - {_html.escape(camp['name'])}", body, "/admin/campaigns", session)


@router.post("/admin/campaigns/{cid}/retry")
def campaign_retry(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import retry_failed
    try:
        retry_failed(cid)
    except Exception:
        pass
    return RedirectResponse(f"/admin/campaigns/{cid}/analytics", status_code=303)


@router.post("/admin/campaigns/{cid}/pause")
def campaign_pause(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import pause_campaign
    try:
        pause_campaign(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/campaigns", status_code=303)


@router.post("/admin/campaigns/{cid}/resume")
def campaign_resume(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import resume_campaign
    try:
        resume_campaign(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/campaigns", status_code=303)


@router.post("/admin/campaigns/{cid}/clone")
def campaign_clone(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import clone_campaign
    try:
        result = clone_campaign(cid)
        return RedirectResponse(f"/admin/campaigns/{result['id']}/recipients", status_code=303)
    except Exception:
        return RedirectResponse("/admin/campaigns", status_code=303)


@router.get("/admin/settings/sending", response_class=HTMLResponse)
def sending_settings_view(session=Depends(require_session), ok: str = ""):
    """v3.22 send-strategy config: daily window, senders, rush, batch size."""
    from mailchad.terminal import settings as _s
    def _val(k, d):
        v = _s.get(k, None)
        return v if v not in (None, "") else str(d)
    FIELDS = [
        ("send_window_tz", "Window timezone", "IANA tz, e.g. America/Los_Angeles", "America/Los_Angeles"),
        ("send_window_start_hour", "Window start hour (local)", "0–23; 9 = 09:00", "9"),
        ("send_window_hours", "Window length (hours)", "e.g. 4 -> a 4-hour window", "4"),
        ("send_sender_count", "Parallel senders", "how many send cursors run in parallel", "3"),
        ("send_jitter_min_s", "Jitter min (sec)", "min gap between a sender's sends", "60"),
        ("send_jitter_max_s", "Jitter max (sec)", "max gap between a sender's sends", "210"),
        ("send_rush_tail_minutes", "End-of-window rush (min)", "final minutes: gap collapses to drain the queue", "30"),
        ("send_rush_jitter_s", "Rush gap (sec)", "tight gap during the rush", "15"),
        ("send_batch_size", "Batch size", "contacts per batch (manual approve-next)", "1000"),
    ]
    def _row(k, label, hint, dflt):
        return f"""
  <form method="post" action="/admin/settings/{k}/save" class="row" style="gap:10px;align-items:center;margin:8px 0;flex-wrap:wrap">
    <label style="width:210px;font-size:13px">{label}</label>
    <input class="input sm" name="value" value="{_html.escape(_val(k, dflt))}" style="max-width:240px">
    <button class="btn ghost sm" type="submit">Save</button>
    <span class="dim" style="font-size:11.5px">{hint}</span>
  </form>"""
    saved = f'<div class="banner ok" style="margin-bottom:12px">Saved <code>{_html.escape(ok)}</code>.</div>' if ok else ""
    body = f"""
<div class="page-head"><div class="titles">
  <div class="eyebrow">Operations · Sending</div>
  <h1 class="page-title">Send window &amp; pacing</h1>
  <p class="page-sub">How batched campaigns spread over time. The terminal compiles a <code>send_at</code> per recipient from these; the cloud fires each when due. Changes apply to the next batch loaded - no redeploy.</p>
</div></div>
{saved}
<div class="card pad mb">{''.join(_row(*f) for f in FIELDS)}</div>
<p class="dim" style="font-size:12.5px">Seeds are managed on the <a class="link" href="/admin/seeds">Seeds</a> page. Batch progress + approve-next live on each campaign's Analytics page.</p>
"""
    return _layout("Send window", body, "/admin/schedule", session)


@router.get("/admin/seeds", response_class=HTMLResponse)
def seeds_view(session=Depends(require_session)):
    """Seed/monitor addresses salted into every batch for inbox-placement checks."""
    from mailchad.terminal import db as _db
    with _db.conn() as c:
        seeds = [dict(r) for r in c.execute(
            "SELECT * FROM seed_addresses ORDER BY id").fetchall()]
        recent = [dict(r) for r in c.execute(
            "SELECT sp.*, cb.campaign_id, cb.batch_no FROM seed_placements sp "
            "LEFT JOIN campaign_batches cb ON cb.id = sp.batch_id "
            "ORDER BY sp.id DESC LIMIT 25").fetchall()]

    def _seed_row(s):
        on = s["active"] == 1
        pill = "ok" if on else ""
        toggle_label = "Disable" if on else "Enable"
        return (f'<tr><td class="cell-strong">{_html.escape(s["email"])}</td>'
                f'<td class="cell-dim">{_html.escape(s.get("provider") or "-")}</td>'
                f'<td><span class="pill {pill}"><span class="dot"></span>{"active" if on else "off"}</span></td>'
                f'<td class="row" style="gap:6px">'
                f'<button class="btn ghost sm" onclick="toggleSeed({s["id"]})">{toggle_label}</button>'
                f'<button class="btn danger sm" onclick="delSeed({s["id"]})">Delete</button></td></tr>')
    seed_rows = "".join(_seed_row(s) for s in seeds) or \
        '<tr><td colspan=4 class="empty" style="padding:28px">No seeds yet - add monitored inboxes across providers.</td></tr>'

    P_PILL = {"inbox":"ok","spam":"crit","promotions":"warn","missing":"crit","unknown":""}
    def _place_row(p):
        return (f'<tr><td class="cell-dim">{(p["checked_at"] or "")[:16]}</td>'
                f'<td class="cell-strong">{_html.escape(p["seed_email"])}</td>'
                f'<td><span class="pill {P_PILL.get(p["placement"],"")}"><span class="dot"></span>{p["placement"]}</span></td>'
                f'<td class="cell-dim">{("camp "+str(p["campaign_id"])+" · batch "+str(p["batch_no"])) if p.get("campaign_id") else "-"}</td></tr>')
    place_rows = "".join(_place_row(p) for p in recent) or \
        '<tr><td colspan=4 class="empty" style="padding:24px">No placement logged yet.</td></tr>'

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Operations</div>
    <h1 class="page-title">Seed addresses</h1>
    <p class="page-sub">Known monitor inboxes salted into <b>every</b> batch so you can verify inbox vs spam placement. Excluded from analytics, suppression and the per-day lock.</p>
  </div>
</div>

<div class="card pad mb">
  <div class="row" style="gap:8px;flex-wrap:wrap;align-items:center">
    <input id="seedEmail" class="input" placeholder="seed@gmail.com" style="max-width:280px">
    <input id="seedProvider" class="input" placeholder="provider (gmail, outlook…)" style="max-width:200px">
    <button class="btn" onclick="addSeed()">Add seed</button>
  </div>
</div>

<div class="section-title">Seeds <span class="count">· {len(seeds)}</span></div>
<div class="table-wrap mb">
  <table class="tbl">
    <thead><tr><th>Email</th><th>Provider</th><th>Status</th><th></th></tr></thead>
    <tbody>{seed_rows}</tbody>
  </table>
</div>

<div class="section-title">Recent placement checks</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>Checked</th><th>Seed</th><th>Placement</th><th>Where</th></tr></thead>
    <tbody>{place_rows}</tbody>
  </table>
</div>

<script>
function addSeed(){{
  var e=document.getElementById('seedEmail').value.trim(), p=document.getElementById('seedProvider').value.trim();
  if(!e){{return;}}
  fetch('/admin/seeds',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{email:e,provider:p||null}})}})
    .then(function(r){{ if(!r.ok){{return r.json().then(function(j){{alert(j.detail||'add failed');}});}} _reloadKeepScroll(); }});
}}
function toggleSeed(id){{ fetch('/admin/seeds/'+id+'/toggle',{{method:'POST'}}).then(function(){{_reloadKeepScroll();}}); }}
function delSeed(id){{ if(!confirm('Delete this seed?'))return; fetch('/admin/seeds/'+id,{{method:'DELETE'}}).then(function(){{_reloadKeepScroll();}}); }}
</script>
"""
    return _layout("Seeds", body, "/admin/seeds", session)


@router.get("/admin/contacts/export")
def contacts_export(session=Depends(require_session), ids: str = ""):
    from mailchad.terminal.routes_admin import export_contacts
    return export_contacts(ids=ids or None)


@router.get("/admin/templates/export")
def templates_export(session=Depends(require_session), ids: str = ""):
    from mailchad.terminal.routes_admin import export_templates
    return export_templates(ids=ids or None)


@router.post("/admin/templates/bulk-import")
async def templates_bulk_import(session=Depends(require_session), file: UploadFile = File(...)):
    from mailchad.terminal.routes_admin import bulk_import_templates
    from fastapi.responses import JSONResponse
    result = await bulk_import_templates(file)
    return JSONResponse(result)


@router.get("/admin/entities/export")
def entities_export(session=Depends(require_session), ids: str = ""):
    from mailchad.terminal.routes_admin import export_entities
    return export_entities(ids=ids or None)


@router.post("/admin/entities/bulk-import")
async def entities_bulk_import(session=Depends(require_session), file: UploadFile = File(...)):
    from mailchad.terminal.routes_admin import bulk_import_entities
    from fastapi.responses import JSONResponse
    result = await bulk_import_entities(file)
    return JSONResponse(result)


@router.get("/admin/drift", response_class=HTMLResponse)
def drift_view(session=Depends(require_session)):
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM drift_report ORDER BY severity='CRITICAL' DESC, detected_at DESC LIMIT 200"
        ).fetchall()
    drift_crit_ct = sum(1 for r in rows if r["severity"] == "CRITICAL" and not r["acknowledged_at"])
    drift_warn_ct = sum(1 for r in rows if r["severity"] == "WARN" and not r["acknowledged_at"])
    check_svg = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>'
    def _drift_row(r) -> str:
        sev_pill = (f'<span class="pill crit"><span class="dot"></span>CRITICAL</span>'
                    if r["severity"] == "CRITICAL" else
                    f'<span class="pill warn"><span class="dot"></span>warning</span>')
        act = (f'<span class="pill ok">{check_svg} acked</span>' if r["acknowledged_at"] else
               f'<form method="post" action="/sync/drift/{r["id"]}/ack" style="display:inline"><button class="btn sm">Acknowledge</button></form>')
        return (f'<tr><td class="cell-dim mono">{r["id"]}</td>'
                f'<td>{sev_pill}</td>'
                f'<td class="cell-strong mono">{_html.escape(r["category"])}</td>'
                f'<td class="cell-mono">{_html.escape((r["job_id"] or "")[:12])}</td>'
                f'<td class="cell-dim">{r["detected_at"]}</td>'
                f'<td class="actions">{act}</td></tr>')
    table_rows = "".join(_drift_row(r) for r in rows) or '<tr><td colspan=6 class="empty" style="padding:32px">No drift entries.</td></tr>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Monitoring</div>
    <h1 class="page-title">Drift report</h1>
    <p class="page-sub">Delivery anomalies detected during sync. Critical entries surface first. Acknowledge once reviewed.</p>
  </div>
  <div class="head-actions">
    {'<span class="pill crit"><span class="dot"></span>' + str(drift_crit_ct) + ' critical</span>' if drift_crit_ct else ''}
    {'<span class="pill warn"><span class="dot"></span>' + str(drift_warn_ct) + ' warnings</span>' if drift_warn_ct else ''}
  </div>
</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>ID</th><th>Severity</th><th>Category</th><th>Job</th><th>Detected</th><th class="actions"></th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>
"""
    return _layout("Drift Report", body, "/admin/drift", session)


# entities

def _entities_config() -> str:
    """Config section rendered at the bottom of the Entities page."""
    import os, httpx
    from mailchad.terminal import encryption as _enc

    brand_rows = (
        _setting_row("entity_name", "Entity name", help="Shown on unsubscribe + erasure pages") +
        _setting_row("support_email", "Support email", help="Fallback contact on unsubscribe pages") +
        _setting_row("public_host", "Public hostname", help="Base URL for unsub/erasure links in emails") +
        _setting_row("email_footer_address", "Postal address (CAN-SPAM)",
                     help="Required for promotional sends - appears as email footer text") +
        _setting_row("email_from", "Global sender fallback",
                     help="Used when no per-company from-email is set") +
        _setting_row("resend_api_key", "Global Resend API key (fallback)", is_secret=True, kind="password",
                     help="Used when a campaign has no company, or the company has no key set")
    )

    # Cloud cross-call: fetch cloud's settings if bearer is available
    CLOUD_URL = os.environ.get("CLOUD_URL", "http://cloud:8443")
    bearer_path = _enc.KEYS_DIR / "cloud_bearer.txt"
    bearer = bearer_path.read_text().strip() if bearer_path.exists() else ""

    cloud_html = ""
    if bearer:
        CLOUD_KNOWN = [
            ("resend_webhook_secret", "Resend webhook secret", True,
             "HMAC-SHA256 key from Resend dashboard -> webhook config"),
            ("webhook_max_skew_s", "Webhook replay window (s)", False,
             "Default 600. Lower = stricter clock-skew tolerance"),
            ("entity_name", "Entity name (cloud copy)", False, "Shown on cloud's unsubscribe page"),
            ("support_email", "Support email (cloud copy)", False, "Fallback contact on cloud's unsub page"),
            ("public_host", "Public host (cloud copy)", False, "Used in cloud-generated URLs"),
        ]
        try:
            r = httpx.get(f"{CLOUD_URL}/settings",
                          headers={"Authorization": f"Bearer {bearer}"}, timeout=8)
            r.raise_for_status()
            data = r.json()
            cloud_settings = data.get("settings", {})
            cloud_rows = ""
            for key, label, is_sec, help_text in CLOUD_KNOWN:
                cur = cloud_settings.get(key, "")
                val_display = (
                    "<span style='background:#fdc;color:#840;padding:.1em .45em;border-radius:3px;font-size:.8em'>encrypted</span>"
                    if is_sec and cur else
                    f"<code style='font-size:.88em'>{_html.escape(str(cur)[:60])}</code>" if cur else
                    "<span style='background:#eee;color:#888;padding:.1em .45em;border-radius:3px;font-size:.8em'>not set</span>"
                )
                cloud_rows += f"""
<tr>
  <th style='width:32%;font-weight:600;font-size:.9em;vertical-align:top;padding:.5em .8em'>{label}<br><code style='color:#aaa;font-size:.8em;font-weight:400'>{key}</code></th>
  <td style='padding:.5em .8em'>
    <div style='margin-bottom:.3em'>{val_display}</div>
    <form method='post' action='/admin/settings/cloud/save' style='display:flex;gap:.4em'>
      <input type='hidden' name='key' value='{key}'>
      <input type='{"password" if is_sec else "text"}' name='value' placeholder='{"paste new value" if is_sec else (str(cur) or "")}' style='flex:1'>
      <button type='submit' style='white-space:nowrap'>Save to cloud</button>
    </form>
    <div style='color:#888;font-size:.82em;margin-top:.2em'>{help_text}</div>
  </td>
</tr>"""
            cloud_html = _config_section("Cloud settings", cloud_rows)
        except Exception as e:
            cloud_html = f"<p style='color:#888;font-size:.88em;margin-top:.5em'>Cloud unreachable: {_html.escape(str(e))}</p>"
    else:
        cloud_html = "<p style='color:#888;font-size:.88em;margin-top:.5em'>Run <code>bin/v3 init-handshake</code> to unlock cloud settings.</p>"

    return _config_section("Brand &amp; sender config", brand_rows) + cloud_html


@router.get("/admin/entities", response_class=HTMLResponse)
def entities_view(session=Depends(require_session)):
    with db.conn() as c:
        rows = c.execute("SELECT * FROM entities ORDER BY name").fetchall()

    close_x = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>'
    lock_svg = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
    cards = ""
    for r in rows:
        key_set = bool(r["resend_key_enc"])
        badge = (f'<span class="key-badge set">{lock_svg} Key set</span>' if key_set
                 else '<span class="key-badge unset">No key</span>')
        from_line = _html.escape(r["from_email"] or f"hello@{r['domain']}")
        cards += f"""<div class="card pad" id="co-{r['id']}">
  <div class="row between" style="align-items:flex-start;margin-bottom:8px">
    <div style="display:flex;align-items:flex-start;gap:8px">
      <input type="checkbox" value="{r['id']}" onchange="entityToggle({r['id']},this)" style="margin-top:3px;width:14px;height:14px;accent-color:var(--accent)" title="Select for export">
      <div>
        <h3 style="font-size:14px;margin-bottom:3px">{_html.escape(r['name'])}</h3>
        <div class="cell-dim" style="font-size:12.5px">🌐 {_html.escape(r['domain'])}</div>
      </div>
    </div>
    {badge}
  </div>
  <div class="dim" style="font-size:12.5px;margin:.3em 0">From: <b class="cell-strong">{_html.escape(r['from_name'] or r['name'])}</b> &lt;{from_line}&gt;</div>
  <div class="dim" style="font-size:12px;margin-bottom:10px">Created {r['created_at'][:10]}</div>
  <div class="row wrap" style="gap:6px">
    <button class="btn sm" onclick='openEdit({r["id"]},{_html.escape(repr(r["name"]))},{_html.escape(repr(r["domain"]))},{_html.escape(repr(r["from_name"]))},{_html.escape(repr(r["from_email"]))})'>Edit</button>
    <button class="btn sm" onclick='openKey({r["id"]},{_html.escape(repr(r["name"]))})'>{"Update key" if key_set else "Set key"}</button>
    <form method="post" action="/admin/entities/{r['id']}/delete" style="margin:0" onsubmit='return confirm("Delete {_html.escape(r["name"])}?")'>
      <button class="btn sm danger">Delete</button>
    </form>
  </div>
</div>"""

    entity_ids_js = "[" + ",".join(str(r["id"]) for r in rows) + "]"
    empty = '<div class="empty" style="grid-column:1/-1"><h4>No entities yet</h4><p>Add one to send from multiple domains.</p></div>' if not rows else ""
    plus_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>'
    dl_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
    up_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>'

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Multi-sender</div>
    <h1 class="page-title">Entities</h1>
    <p class="page-sub">Each entity has its own sending domain and Resend key. Per-entity keys take precedence over the global fallback.</p>
  </div>
  <div class="head-actions">
    <a class="btn ghost sm" href="/admin/entities/export">{dl_svg} Export all</a>
    <button class="btn ghost sm" onclick="entityExportSelected()" id="entity-export-sel" style="display:none">Export selected</button>
    {_help_btn("entity-export-help")}
    {_help_btn("entity-import-help")}
    <button class="btn primary" onclick="openAdd()">{plus_svg} Add entity</button>
  </div>
</div>

<div class="grid" id="entity-grid" style="grid-template-columns:repeat(auto-fill,minmax(300px,1fr))">{cards}{empty}</div>
<script>
var _entitySelected=new Set();
function entityToggle(id,el){{
  if(el.checked)_entitySelected.add(id); else _entitySelected.delete(id);
  document.getElementById('entity-export-sel').style.display=_entitySelected.size?'':'none';
}}
function entityExportSelected(){{
  if(!_entitySelected.size)return;
  window.location='/admin/entities/export?ids='+Array.from(_entitySelected).join(',');
}}
</script>

<!-- entity import form (inline, collapsible) -->
<details class="card pad" style="margin-top:20px">
  <summary style="cursor:pointer;font-size:13px;font-weight:600;list-style:none;display:flex;align-items:center;gap:8px">{up_svg} Import entities from JSON</summary>
  <div style="margin-top:14px">
    <p class="dim" style="margin-bottom:12px;font-size:13px">Upload a JSON file exported from another instance. <b>Resend API keys are not included</b> - set them manually after import.</p>
    <div id="entity-import-result"></div>
    <div class="field"><input type="file" id="entity-import-file" accept=".json,application/json"></div>
    <div class="form-actions"><button class="btn" onclick="entityImport()">Import</button><span id="entity-import-status" class="dim" style="font-size:12.5px"></span></div>
  </div>
</details>
<script>
async function entityImport(){{
  var f=document.getElementById('entity-import-file').files[0];
  if(!f)return;
  var st=document.getElementById('entity-import-status');
  st.textContent='Importing…';
  var fd=new FormData(); fd.append('file',f);
  var r=await fetch('/admin/entities/bulk-import',{{method:'POST',body:fd}});
  var d=await r.json();
  st.textContent='';
  var res=document.getElementById('entity-import-result');
  if(d.errors&&d.errors.length)
    res.innerHTML='<div class="callout crit" style="margin-bottom:12px">Imported '+d.imported+', skipped '+d.skipped_duplicate+' duplicates. Errors:<br>'+d.errors.slice(0,5).join('<br>')+'</div>';
  else
    res.innerHTML='<div class="callout ok" style="margin-bottom:12px">Imported '+d.imported+' entities. Skipped '+d.skipped_duplicate+' duplicates. Set Resend keys manually.</div>';
  if(d.imported>0)setTimeout(function(){{_reloadKeepScroll();}},1500);
}}
</script>

{_help_overlay("entity-export-help", "Entity export format",
    '<p class="dim" style="margin-bottom:12px">Exports a <b>JSON array</b>. <b>Resend API keys are excluded</b> for security - they must be set manually after import.</p>'
    '<pre>[\n  {{\n    "name": "Acme Studio",\n    "domain": "mail.acme.io",\n    "from_name": "Acme",\n    "from_email": "hello@mail.acme.io",\n    "footer_address": "42 Main St",\n    "support_email": "support@acme.io",\n    "public_host": "mail.acme.io"\n  }}\n]</pre>'
    '<p class="dim" style="font-size:12px">Export all or select cards using the checkbox that appears on hover.</p>'
)}
{_help_overlay("entity-import-help", "Entity import format",
    '<p class="dim" style="margin-bottom:12px">Upload the JSON file produced by Export. All fields except <code class="code">resend_key_enc</code> are imported.</p>'
    '<table class="field-table"><thead><tr><th>Field</th><th>Required</th><th>Notes</th></tr></thead><tbody>'
    '<tr><td><code class="code">name</code></td><td><span class="req">Required</span></td><td>Unique entity name</td></tr>'
    '<tr><td><code class="code">domain</code></td><td><span class="req">Required</span></td><td>Sending domain (must be verified in Resend)</td></tr>'
    '<tr><td><code class="code">from_name</code></td><td>Optional</td><td>Display name in From header</td></tr>'
    '<tr><td><code class="code">from_email</code></td><td>Optional</td><td>From address</td></tr>'
    '<tr><td><code class="code">footer_address</code></td><td>Optional</td><td>CAN-SPAM physical address</td></tr>'
    '<tr><td><code class="code">support_email</code></td><td>Optional</td><td>Support contact</td></tr>'
    '<tr><td><code class="code">public_host</code></td><td>Optional</td><td>Base URL for unsub/erasure links</td></tr>'
    '</tbody></table>'
    '<p class="dim" style="font-size:12px;margin-top:8px">Duplicate names are skipped. After import, set the Resend key on each entity.</p>'
)}

<!-- Add modal -->
<div class="modal-overlay" id="modal-add" onclick="if(event.target===this)closeModals()">
<div class="modal">
  <div class="modal-head"><h3>Add company</h3><button class="icon-btn" onclick="closeModals()">{close_x}</button></div>
  <div class="modal-body">
    <form method="post" action="/admin/entities/create" id="add-form">
      <div class="field"><label>Entity name</label><input type="text" name="name" required autofocus placeholder="Acme Studio"></div>
      <div class="field"><label>Sending domain</label><input type="text" name="domain" required class="mono" placeholder="mail.acme.io"></div>
      <div class="form-grid">
        <div class="field"><label>From name</label><input type="text" name="from_name" placeholder="Acme"></div>
        <div class="field"><label>From email</label><input type="email" name="from_email" placeholder="hello@mail.acme.io"></div>
      </div>
    </form>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="closeModals()">Cancel</button>
    <button class="btn primary" onclick="document.getElementById('add-form').submit()">Create company</button>
  </div>
</div>
</div>

<!-- Edit modal -->
<div class="modal-overlay" id="modal-edit" onclick="if(event.target===this)closeModals()">
<div class="modal">
  <div class="modal-head"><h3>Edit company</h3><button class="icon-btn" onclick="closeModals()">{close_x}</button></div>
  <div class="modal-body">
    <form method="post" id="edit-form" action="">
      <div class="field"><label>Entity name</label><input type="text" name="name" id="edit-name" required></div>
      <div class="field"><label>Sending domain</label><input type="text" name="domain" id="edit-domain" required class="mono"></div>
      <div class="form-grid">
        <div class="field"><label>From name</label><input type="text" name="from_name" id="edit-from_name"></div>
        <div class="field"><label>From email</label><input type="email" name="from_email" id="edit-from_email"></div>
      </div>
    </form>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="closeModals()">Cancel</button>
    <button class="btn primary" onclick="document.getElementById('edit-form').submit()">Save</button>
  </div>
</div>
</div>

<!-- Set key modal -->
<div class="modal-overlay" id="modal-key" onclick="if(event.target===this)closeModals()">
<div class="modal">
  <div class="modal-head"><h3 id="key-modal-title">Set Resend API key</h3><button class="icon-btn" onclick="closeModals()">{close_x}</button></div>
  <div class="modal-body">
    <div class="callout warn" style="margin-bottom:14px">
      {lock_svg}
      <div class="ct-body">The key is encrypted at rest and <b>never shown again</b> after save.</div>
    </div>
    <form method="post" id="key-form" action="">
      <div class="field"><label>Resend API key</label><input type="password" name="resend_api_key" id="key-input" required autocomplete="off" class="mono" placeholder="re_••••••••••••••••"></div>
    </form>
  </div>
  <div class="modal-foot" style="flex-direction:column;align-items:stretch;gap:10px">
    <div class="row" style="gap:8px;align-items:center">
      <button class="btn ghost sm" id="test-btn" data-id="" onclick="testConnection()">Test connection</button>
      <span id="test-result" style="font-size:12.5px;flex:1"></span>
    </div>
    <div class="row" style="justify-content:flex-end;gap:8px">
      <button class="btn ghost" onclick="closeModals()">Cancel</button>
      <button class="btn primary" onclick="document.getElementById('key-form').submit()">Save key</button>
    </div>
  </div>
</div>
</div>

<script>
function closeModals(){{document.querySelectorAll('.modal-overlay').forEach(el=>el.classList.remove('open'));}}
function openAdd(){{closeModals();document.getElementById('modal-add').classList.add('open');}}
function openEdit(id,name,domain,fromName,fromEmail){{closeModals();document.getElementById('edit-form').action='/admin/entities/'+id+'/update';document.getElementById('edit-name').value=name;document.getElementById('edit-domain').value=domain;document.getElementById('edit-from_name').value=fromName;document.getElementById('edit-from_email').value=fromEmail;document.getElementById('modal-edit').classList.add('open');}}
function openKey(id,name){{closeModals();document.getElementById('key-modal-title').textContent='Set Resend key - '+name;document.getElementById('key-form').action='/admin/entities/'+id+'/set-key';document.getElementById('key-input').value='';document.getElementById('modal-key').classList.add('open');document.getElementById('test-result').textContent='';document.getElementById('test-btn').dataset.id=id;}}
async function testConnection(){{
  var id=document.getElementById('test-btn').dataset.id;
  var r=document.getElementById('test-result');
  r.textContent='Testing…'; r.style.color='var(--text-muted)';
  try{{
    var res=await fetch('/admin/entities/'+id+'/test-connection',{{method:'POST'}});
    var d=await res.json();
    r.textContent=d.detail;
    r.style.color=d.ok?'var(--ok-fg)':'var(--crit-fg)';
  }}catch(e){{r.textContent='Error: '+e;r.style.color='var(--crit-fg)';}}
}}
</script>
{_entities_config()}
"""
    return _layout("Entities", body, "/admin/entities", session)


@router.post("/admin/entities/{cid}/test-connection")
async def entity_test_connection(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import test_entity_connection
    from fastapi.responses import JSONResponse
    return JSONResponse(test_entity_connection(cid))


@router.post("/admin/entities/create")
def entities_create(session=Depends(require_session),
                     name: str = Form(...), domain: str = Form(...),
                     from_name: str = Form(""), from_email: str = Form("")):
    from mailchad.terminal.routes_admin import create_entity, EntityIn
    try:
        create_entity(EntityIn(name=name, domain=domain, from_name=from_name, from_email=from_email))
    except Exception:
        pass
    return RedirectResponse("/admin/entities", status_code=303)


@router.post("/admin/entities/{cid}/update")
def entities_update(cid: int, session=Depends(require_session),
                     name: str = Form(...), domain: str = Form(...),
                     from_name: str = Form(""), from_email: str = Form("")):
    from mailchad.terminal.routes_admin import update_entity, EntityIn
    try:
        update_entity(cid, EntityIn(name=name, domain=domain, from_name=from_name, from_email=from_email))
    except Exception:
        pass
    return RedirectResponse("/admin/entities", status_code=303)


@router.post("/admin/entities/{cid}/set-key")
def entities_set_key(cid: int, session=Depends(require_session),
                      resend_api_key: str = Form(...)):
    from mailchad.terminal.routes_admin import set_entity_key, EntityKeyIn
    try:
        set_entity_key(cid, EntityKeyIn(resend_api_key=resend_api_key))
    except Exception:
        pass
    return RedirectResponse("/admin/entities", status_code=303)


@router.post("/admin/entities/{cid}/delete")
def entities_delete(cid: int, session=Depends(require_session)):
    from mailchad.terminal.routes_admin import delete_company
    try:
        delete_company(cid)
    except Exception:
        pass
    return RedirectResponse("/admin/entities", status_code=303)


# suppression

@router.get("/admin/suppression", response_class=HTMLResponse)
def suppression_view(session=Depends(require_session), reason: str = ""):
    import hashlib as _hl
    with db.conn() as c:
        if reason:
            sup_rows = c.execute(
                "SELECT email_hash, reason, source, added_at FROM suppression_hashes WHERE reason=? ORDER BY added_at DESC LIMIT 500",
                (reason,),
            ).fetchall()
        else:
            sup_rows = c.execute(
                "SELECT email_hash, reason, source, added_at FROM suppression_hashes ORDER BY added_at DESC LIMIT 500"
            ).fetchall()
        contacts = c.execute("SELECT email FROM contacts").fetchall()

    # Build hash->email lookup
    h2email: dict[str, str] = {}
    for row in contacts:
        em = row["email"]
        h2email[_hl.sha256(em.lower().encode()).hexdigest()] = em

    REASON_PILL = {"unsubscribe": "", "complaint": "crit", "bounce_hard": "warn",
                   "manual": "", "erasure_request": "info"}
    reason_filter = reason
    filter_opts = "".join(
        f"<option value='{v}'{' selected' if reason_filter == v else ''}>{v}</option>"
        for v in ("unsubscribe", "complaint", "bounce_hard", "manual", "erasure_request")
    )
    def _sup_row(r) -> str:
        rp = REASON_PILL.get(r["reason"], "")
        em = h2email.get(r["email_hash"], "")
        return (f'<tr>'
                f'<td>{_html.escape(em) if em else ("<span class=\"cell-mono\">" + r["email_hash"][:16] + "…</span>")}</td>'
                f'<td><span class="pill {rp}"><span class="dot"></span>{r["reason"]}</span></td>'
                f'<td class="cell-dim">{r["source"]}</td>'
                f'<td class="cell-dim">{(r["added_at"] or "")[:16]}</td>'
                f'</tr>')
    table = "".join(_sup_row(r) for r in sup_rows) or \
        '<tr><td colspan=4 class="empty" style="padding:32px">No suppression entries.</td></tr>'

    with db.conn() as c2:
        total_sup = c2.execute("SELECT count(*) FROM suppression_hashes").fetchone()[0]
        unsub_ct = c2.execute("SELECT count(*) FROM suppression_hashes WHERE reason='unsubscribe'").fetchone()[0]
        bounce_ct = c2.execute("SELECT count(*) FROM suppression_hashes WHERE reason='bounce_hard'").fetchone()[0]
        complaint_ct = c2.execute("SELECT count(*) FROM suppression_hashes WHERE reason='complaint'").fetchone()[0]
        erasure_ct = c2.execute("SELECT count(*) FROM suppression_hashes WHERE reason='erasure_request'").fetchone()[0]
    dl_svg = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Compliance</div>
    <h1 class="page-title">Suppression</h1>
    <p class="page-sub">Emails are stored as SHA-256 hashes. Known addresses are resolved against your contact list; unmatched hashes show as -.</p>
  </div>
  <div class="head-actions">
    <a class="btn" href="/admin/suppression/csv">{dl_svg} Export CSV</a>
  </div>
</div>

<div class="stat-strip mb">
  <div class="stat"><div class="k">Total suppressed</div><div class="v">{total_sup}</div></div>
  <div class="stat"><div class="k">Unsubscribes</div><div class="v">{unsub_ct}</div></div>
  <div class="stat"><div class="k">Hard bounces</div><div class="v{"" if not bounce_ct else " warn"}">{bounce_ct}</div></div>
  <div class="stat"><div class="k">Complaints</div><div class="v{"" if not complaint_ct else " crit"}">{complaint_ct}</div></div>
  <div class="stat"><div class="k">Erasure requests</div><div class="v">{erasure_ct}</div></div>
</div>

<div class="row between mb" style="flex-wrap:wrap;gap:12px">
  <div class="section-title" style="margin:0">Suppression list</div>
  <form method="get" action="/admin/suppression" style="display:flex;gap:8px;align-items:center">
    <select name="reason" style="width:200px">
      <option value=""{' selected' if not reason_filter else ''}>All reasons</option>
      {filter_opts}
    </select>
    <button class="btn sm" type="submit">Filter</button>
    {'<a class="btn ghost sm" href="/admin/suppression">Clear</a>' if reason_filter else ''}
  </form>
</div>
<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th>Email / hash</th><th>Reason</th><th>Source</th><th>Added</th></tr></thead>
    <tbody>{table}</tbody>
  </table>
</div>
<p class="dim mt">Showing up to 500 entries. Emails shown only for contacts in your local DB.</p>
{_config_section("Suppression config",
    '<div class="callout warn" style="margin-bottom:16px">⚠ Set these secrets before sending to real recipients. Once set and emails are sent, <b>do not change them</b> - existing unsubscribe links will break. Generate a random value and save.'
    '<div style="margin-top:10px"><button class="btn sm" onclick="generateHmacSecrets()">Generate both secrets</button></div></div>'
    '<script>function generateHmacSecrets(){{var a=Array.from(crypto.getRandomValues(new Uint8Array(32))).map(b=>b.toString(16).padStart(2,"0")).join("");var b=Array.from(crypto.getRandomValues(new Uint8Array(32))).map(b=>b.toString(16).padStart(2,"0")).join("");var f1=document.querySelector(\'input[name="unsub_secret"]\');var f2=document.querySelector(\'input[name="erasure_secret"]\');if(f1)f1.value=a;if(f2)f2.value=b;}}</script>' +
    _setting_row("unsub_secret", "Unsubscribe signing secret", is_secret=True,
                 help="HMAC key used to sign unsubscribe tokens. Sync'd to cloud automatically on save.") +
    _setting_row("erasure_secret", "Erasure request signing secret", is_secret=True,
                 help="HMAC key used to sign erasure request tokens. Sync'd to cloud on save.") +
    _setting_row("unsub_token_ttl_s", "Unsubscribe token TTL (seconds)", kind="number",
                 help="Default: 604800 (7 days). How long an unsubscribe link stays valid.") +
    _setting_row("erasure_token_ttl_s", "Erasure token TTL (seconds)", kind="number",
                 help="Default: 86400 (24 hours). How long an erasure request link stays valid.")
)}
"""
    return _layout("Suppression List", body, "/admin/suppression", session)


@router.get("/admin/suppression/csv")
def suppression_csv(session=Depends(require_session)):
    import hashlib as _hl
    from fastapi.responses import Response
    with db.conn() as c:
        sup_rows = c.execute(
            "SELECT email_hash, reason, source, added_at FROM suppression_hashes ORDER BY added_at DESC"
        ).fetchall()
        contacts = c.execute("SELECT email FROM contacts").fetchall()
    h2email: dict[str, str] = {}
    for row in contacts:
        em = row["email"]
        h2email[_hl.sha256(em.lower().encode()).hexdigest()] = em

    lines = ["email,email_hash,reason,source,added_at"]
    for r in sup_rows:
        em = h2email.get(r["email_hash"], "")
        lines.append(f"{em},{r['email_hash']},{r['reason']},{r['source']},{r['added_at']}")
    return Response("\n".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=suppression.csv"})


# operators

@router.get("/admin/operators", response_class=HTMLResponse)
def operators_view(session=Depends(require_session)):
    from mailchad.terminal.auth import require_admin
    from fastapi import Request as _Request
    # role check
    if session.get("role", "admin") != "admin":
        return HTMLResponse("<p>Admin access required.</p>", status_code=403)
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, email, role, active, created_at, last_login_at FROM operators ORDER BY id"
        ).fetchall()

    current_email = session.get("sub", "")
    ROLE_PILL = {"admin": "accent", "operator": "info"}

    def _op_row(r) -> str:
        rp = ROLE_PILL.get(r["role"], "")
        you = " <span class='dim'>(you)</span>" if r["email"] == current_email else ""
        is_self = r["email"] == current_email
        toggle_label = "Deactivate" if r["active"] else "Activate"
        toggle_btn = (
            f'<form method="post" action="/admin/operators/{r["id"]}/toggle" style="display:inline">'
            f'<button class="btn sm">{toggle_label}</button></form>'
        ) if not is_self else ""
        del_btn = (
            f'<form method="post" action="/admin/operators/{r["id"]}/delete" style="display:inline"'
            f' onsubmit="return confirm(\'Delete {_html.escape(r["email"])}?\')">'
            f'<button class="btn sm danger">Delete</button></form>'
        ) if not is_self else ""
        active_pill = ('<span class="pill ok"><span class="dot"></span>active</span>' if r["active"]
                       else '<span class="pill"><span class="dot"></span>inactive</span>')
        return (f'<tr>'
                f'<td class="cell-dim mono">{r["id"]}</td>'
                f'<td class="cell-strong">{_html.escape(r["email"])}{you}</td>'
                f'<td><span class="pill {rp}">{r["role"]}</span></td>'
                f'<td class="cell-dim">{(r["last_login_at"] or "-")[:16]}</td>'
                f'<td>{active_pill}</td>'
                f'<td class="actions"><div class="row-actions">{toggle_btn}{del_btn}</div></td>'
                f'</tr>')
    tbl_rows = "".join(_op_row(r) for r in rows) or \
        '<tr><td colspan=6 class="empty" style="padding:32px">No operators yet.</td></tr>'
    close_x = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>'
    plus_svg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5v14"/></svg>'

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Team · admin only</div>
    <h1 class="page-title">Operators</h1>
    <p class="page-sub">Accounts with access to this vault. Admins manage operators and roles; operators can run campaigns but not change team membership.</p>
  </div>
  <div class="head-actions">
    <button class="btn primary" onclick="document.getElementById('add-overlay').classList.add('open')">{plus_svg} Add operator</button>
  </div>
</div>

<div class="table-wrap">
  <table class="tbl">
    <thead><tr><th style="width:38px">ID</th><th>Email</th><th>Role</th><th>Last login</th><th>Status</th><th class="actions"></th></tr></thead>
    <tbody>{tbl_rows}</tbody>
  </table>
</div>

<div class="modal-overlay" id="add-overlay" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
  <div class="modal-head"><h3>Add operator</h3><button class="icon-btn" onclick="document.getElementById('add-overlay').classList.remove('open')">{close_x}</button></div>
  <div class="modal-body">
    <form method="post" action="/admin/operators/create" id="add-op-form">
      <div class="field"><label>Email</label><input type="email" name="email" required placeholder="teammate@example.com"></div>
      <div class="field"><label>Password <span class="hint">≥ 12 chars</span></label><input type="password" name="password" required minlength="12" placeholder="••••••••••••"></div>
      <div class="field" style="margin-bottom:0"><label>Role</label><select name="role"><option value="operator">operator</option><option value="admin">admin</option></select></div>
    </form>
  </div>
  <div class="modal-foot">
    <button class="btn ghost" onclick="document.getElementById('add-overlay').classList.remove('open')">Cancel</button>
    <button class="btn primary" onclick="document.getElementById('add-op-form').submit()">Create operator</button>
  </div>
</div>
</div>
{_config_section("Session config",
    _setting_row("session_ttl_s", "Session TTL (seconds)", kind="number",
                 help="Default: 43200 (12 hours). Cookie-based session lifetime.")
)}
{_config_block("Change admin password", """
<p style='font-size:.9em;color:#555;margin:.3em 0 1em'>Changes take effect immediately. You will remain signed in.</p>
<form method='post' action='/admin/settings/auth/password' style='max-width:380px;margin:0'>
  <label style='display:block;margin:.4em 0 .2em;font-size:.9em'>New password (≥ 12 chars)
    <input type='password' name='new_password' required minlength='12'
           style='display:block;width:100%;margin-top:.2em'></label>
  <label style='display:block;margin:.6em 0 .2em;font-size:.9em'>Confirm password
    <input type='password' name='confirm' required minlength='12'
           style='display:block;width:100%;margin-top:.2em'></label>
  <button type='submit' style='margin-top:.7em'>Update password</button>
</form>
""")}
{_config_block("Rotate JWT secret", """
<p style='font-size:.9em;color:#555;margin:.3em 0 .8em'>Generates a new JWT signing key. All active sessions (including yours) will be invalidated - everyone will need to sign in again.</p>
<form method='post' action='/admin/settings/auth/rotate-jwt'
      onsubmit='return confirm("Rotate JWT secret? All sessions will be signed out.")'>
  <button type='submit' class='danger'>Rotate JWT secret</button>
</form>
""")}
"""
    return _layout("Operators", body, "/admin/operators", session)


@router.post("/admin/operators/create")
def operators_create(session=Depends(require_session),
                     email: str = Form(...), password: str = Form(...), role: str = Form("operator")):
    if session.get("role", "admin") != "admin":
        return RedirectResponse("/admin", status_code=303)
    from mailchad.terminal.routes_admin import create_operator, OperatorIn
    try:
        create_operator(OperatorIn(email=email, password=password, role=role), session)
    except Exception:
        pass
    return RedirectResponse("/admin/operators", status_code=303)


@router.post("/admin/operators/{op_id}/toggle")
def operators_toggle(op_id: int, session=Depends(require_session)):
    if session.get("role", "admin") != "admin":
        return RedirectResponse("/admin", status_code=303)
    from mailchad.terminal.routes_admin import toggle_operator_active
    try:
        toggle_operator_active(op_id)
    except Exception:
        pass
    return RedirectResponse("/admin/operators", status_code=303)


@router.post("/admin/operators/{op_id}/delete")
def operators_delete(op_id: int, session=Depends(require_session)):
    if session.get("role", "admin") != "admin":
        return RedirectResponse("/admin", status_code=303)
    from mailchad.terminal.routes_admin import delete_operator
    try:
        delete_operator(op_id, session)
    except Exception:
        pass
    return RedirectResponse("/admin/operators", status_code=303)


# docs

_DOCS = [
    ("Getting started", "setup onboarding install sign in login", """
Start the terminal with <code>bin/v3 up-terminal</code>.
Sign in at <b>/admin</b> with your operator credentials.
The terminal syncs with the cloud Lambda every 5 seconds once the handshake is complete.
Settings are inline on each feature page - there is no separate Settings tab.
"""),
    ("Handshake", "handshake keypair operator client init bootstrap token", """
Run <code>bin/v3 init-handshake --role operator --cloud &lt;CLOUD_URL&gt; --bootstrap-token &lt;TOKEN&gt;</code>
during a screen-share session with the client. Both sides must run the handshake once.
After handshake, the terminal stores a bearer token and NaCl keypair in its volume.
The bootstrap token is one-time-use and is invalidated after handshake completes.
"""),
    ("Contacts &amp; CSV import", "contacts import csv tags suppression bulk upload", """
Contacts are stored locally in the terminal SQLite DB.
Add individually via the Contacts tab or bulk-import via <b>Upload CSV</b>.
CSV columns: <code>email</code> (required), <code>name</code>, <code>tags</code>, <code>consent_source</code>, <code>external_id</code>.
Tags are pipe-separated strings (e.g. <code>beta|vip</code>). Max 10,000 rows per upload.
Suppressed addresses are skipped automatically during import.
"""),
    ("Contact segmentation", "segment tag filter campaign recipients by-tag bulk add", """
On the Campaign Recipients page, use <b>Add by tag</b> to bulk-add all contacts matching a tag token.
The tag filter on the Contacts page (search box) also narrows the list.
<code>GET /admin/contacts/count?tag=X</code> returns the match count before adding.
Tags are matched with an exact prefix - <code>beta</code> matches contacts tagged <code>beta</code> or <code>beta|vip</code>.
"""),
    ("Templates &amp; preview", "template html subject from-name variable substitution preview render", """
Templates use double-brace variables: <code>{{name}}</code>, <code>{{email}}</code>, <code>{{firstName}}</code>, <code>{{company}}</code>, <code>{{unsubscribeUrl}}</code>, <code>{{previewText}}</code>.
Available variables are listed on the Templates page.
Use the <b>Preview</b> link on any template to see it rendered with sample data before sending.
The inline preview panel on the template detail page updates as you type in the name/email fields.
Templates are encrypted at rest and never leave the vault in plaintext.
"""),
    ("Campaigns", "campaign launch dispatch send promotional transactional scheduled", """
Create a campaign, assign a template and kind (<b>promotional</b> or <b>transactional</b>).
Optionally set a <b>scheduled for</b> datetime - the scheduler loop will launch it automatically.
Mark it tested, then launch. Launch enqueues one job per contact via SQS.
The ep-dispatcher Lambda picks up jobs, decrypts, and POSTs to Resend.
Transactional campaigns bypass suppression lists; promotional ones honour them.
The <b>K_temp TTL</b> setting (Overview page ⚙ Config) controls how long pack decryption keys live.
"""),
    ("Campaign analytics", "analytics opened clicked bounced complained stats rates", """
After dispatch, click <b>Analytics</b> on any campaign to see delivery stats.
Summary row: Sent / Opened / Clicked / Bounced / Complained / Failed with counts and rates.
Per-recipient table shows individual status, timestamps, and message IDs.
Stats are populated by the inbox materialiser - Resend webhook events are synced from the cloud,
decrypted, and applied to <code>campaign_recipients</code> every 10 seconds.
"""),
    ("Scheduled campaigns", "schedule scheduled_for auto-launch timer trigger", """
Set <b>Scheduled for</b> when creating a campaign to queue it for automatic dispatch.
The scheduler loop checks every 60 seconds for campaigns whose <code>scheduled_for</code> is in the past.
On trigger, it calls the same launch path as a manual launch - one job per contact via SQS.
If launch fails, a WARN drift event is written; the campaign remains in <code>scheduled</code> status for retry.
"""),
    ("Re-queue failed sends", "retry failed requeue reset status", """
On a campaign's Analytics page, the <b>Retry Failed</b> button resets all <code>failed</code> recipients back to <code>queued</code>.
The campaign status reverts to <code>tested</code> so you can press Launch again without re-testing.
Only recipients with <code>status='failed'</code> are reset - opened/clicked/bounced are left untouched.
"""),
    ("Suppression &amp; unsubscribes", "suppression unsubscribe hash erasure export csv", """
The Suppression page lists all suppressed addresses (stored as SHA-256 hashes).
Emails are resolved from your local contacts DB where possible; unknown hashes show -.
Filter by reason: <code>unsubscribe</code>, <code>complaint</code>, <code>bounce_hard</code>, <code>manual</code>, <code>erasure_request</code>.
Export the full list as CSV with the <b>Export CSV</b> button.
Suppression secrets and token TTLs live in the ⚙ Suppression config section at the bottom of this page.
"""),
    ("Entities &amp; senders", "company domain resend key multi-tenant per-company sender brand", """
Entities let you send from multiple domains, each with its own Resend API key.
Create a company via the <b>Entities</b> tab: name, domain, from-name, from-email.
Set its Resend key via <b>Set Key</b> - encrypted at rest, never stored in plaintext.
When creating a campaign, select a company from the dropdown to use its key + domain.
If no company is selected, the global <code>resend_api_key</code> setting is used.
Brand settings (company name, support email, global sender) are in ⚙ Brand &amp; sender config at the bottom of the Entities page.
"""),
    ("Multi-operator logins", "operators team login user role admin password invite", """
Manage team members on the <b>Operators</b> page (admin-only).
Each operator has their own email/password and an <code>admin</code> or <code>operator</code> role.
Admins can add, deactivate, or delete other operators; operators cannot.
On startup the platform migrates legacy <code>admin_email</code>/<code>admin_password_hash</code> settings to the operators table.
Session TTL, password change, and JWT rotation are in the ⚙ config sections at the bottom of the Operators page.
"""),
    ("Settings - where to find them", "settings config inline jwt secret password session ttl", """
Settings are now inline on the feature page that owns them - no separate Settings tab.
<ul style='margin:.4em 0;padding-left:1.3em;line-height:1.8'>
  <li><b>Overview</b> ⚙ - K_temp TTL</li>
  <li><b>Entities</b> ⚙ - brand, global sender, Resend API key, cloud cross-call</li>
  <li><b>Suppression</b> ⚙ - unsub/erasure secrets, token TTLs</li>
  <li><b>Operators</b> ⚙ - session TTL, change password, rotate JWT</li>
</ul>
All settings are encrypted at rest in the terminal SQLite DB. On first boot, values are migrated from the <code>.env</code> file.
Old <code>/admin/settings/*</code> URLs redirect to the new locations.
"""),
    ("Sync protocol", "sync pull push cursor bearer token encryption", """
The terminal short-polls <code>GET /sync/pull?since=N</code> every 5 seconds.
Events are encrypted with a NaCl box (terminal pubkey -> cloud privkey) before transit.
The sync cursor advances monotonically; missed events are replayed on reconnect.
Push path: terminal <code>POST /sync/push</code> -> cloud stores event -> Lambda reads on next pull.
Bearer token is stored in <code>KEYS_DIR/cloud_bearer.txt</code> after handshake.
"""),
    ("Inbox materialiser", "materialiser webhook inbox opened clicked bounced", """
Webhook events from Resend (opened/clicked/bounced/complained) are synced from the cloud,
decrypted, and applied to <code>campaign_recipients</code> every 10 seconds by the materialiser loop.
Suppression events (unsubscribe hash synced from cloud) are also applied here.
If the KeyBundle is not yet ready (handshake not run), materialisation is skipped and retried next tick.
Check <code>GET /sync/status</code> for pending inbox event counts.
"""),
    ("Drift detection", "drift critical warn acknowledged category job", """
Drift events are recorded when a dispatched job's outcome differs from the expected state.
CRITICAL drift = delivery failure after all retries. WARN = soft anomaly (e.g. scheduler launch failure).
Acknowledge drift events via the Drift tab or <code>POST /sync/drift/&lt;id&gt;/ack</code>.
Unacked CRITICAL drift appears as a red badge on the Overview.
"""),
    ("Backup", "backup snapshot restore", """
Run <code>bin/v3 backup</code> to create a point-in-time snapshot of the terminal volume.
Snapshots are stored in <code>backups/</code> as encrypted tarballs.
The cloud Lambda holds no plaintext data - the terminal DB is the source of truth.
"""),
    ("Lambda deployment", "lambda deploy aws api gateway sqs dispatcher", """
Deploy the API Lambda: <code>bin/v3 deploy-api</code>.
Deploy the dispatcher Lambda: <code>bin/v3 deploy-dispatcher</code>.
The dispatcher is triggered by SQS; wired automatically on first deploy.
Tail CloudWatch logs with <code>bin/v3 tail-api</code> and <code>bin/v3 tail-dispatcher</code>.
IAM policies are in <code>deploy/iam/</code>.
"""),
]

@router.get("/admin/docs", response_class=HTMLResponse)
def docs_view(session=Depends(require_session)):
    sections = ""
    for title, keywords, content in _DOCS:
        kw = f"{title.lower()} {keywords}"
        sections += f"""<div class='doc-section' data-kw='{kw}'>
<h2>{title}</h2>
<p>{content.strip()}</p>
</div>"""

    body = f"""
<div class="page-head">
  <div class="titles">
    <div class="eyebrow">Operator handbook</div>
    <h1 class="page-title">Docs</h1>
    <p class="page-sub">Everything you need to run the vault. Search filters sections in real time.</p>
  </div>
</div>
<div style="position:relative;margin-bottom:8px">
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="var(--text-dim)" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="position:absolute;left:12px;top:50%;transform:translateY(-50%)"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
  <input type="search" id="q" placeholder="Search the docs…" style="padding-left:36px;font-size:14px;padding-top:10px;padding-bottom:10px;max-width:440px" oninput="filter()">
</div>
<div id="noResults" class="empty" style="display:none">
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
  <h4>No matching sections</h4><div class="dim">Try a different term.</div>
</div>
<div id="docs">{sections}</div>
<script>
function filter(){{
  var q=document.getElementById('q').value.toLowerCase().trim();
  var secs=document.querySelectorAll('.doc-section');
  var hits=0;
  secs.forEach(function(el){{var hide=q!==''&&!el.dataset.kw.includes(q);el.classList.toggle('hidden',hide);if(!hide)hits++;;}});
  document.getElementById('noResults').style.display=(q&&!hits)?'block':'none';
}}
document.getElementById('q').focus();
</script>
"""
    return _layout("Docs", body, "/admin/docs", session)
