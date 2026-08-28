"""Static front-end assets for the dashboard: CSS, icon sprite, script.

Kept out of :mod:`kai_trader.dashboard.render` so the rendering logic stays
readable, and so these three blobs sit verbatim as designed. Nothing here is
templated: render embeds each constant once per document, which is what keeps
every page self-contained with no external asset fetch.
"""

from __future__ import annotations

#: The whole design system: tokens, layout, and every component class.
CSS = r"""
:root{
--ink-900:#17140F;--ink-800:#241F18;--ink-700:#3A332A;--ink-600:#554C3F;--ink-500:#756A5A;
--ink-400:#9C9080;--ink-300:#C2B7A6;--ink-200:#DED4C4;--ink-100:#EFE7DA;--ink-050:#F8F2E8;
--paper:#FFFDF8;--cream:#F6EFE3;--sand:#EADFCB;
--persimmon-600:#C94222;--persimmon-500:#E45A2E;--persimmon-100:#FCE3D8;
--jade-700:#12564C;--jade-600:#17705F;--jade-500:#1E8A73;--jade-100:#DBEFE8;
--butter-600:#D99A16;--butter-500:#F2B31F;--butter-100:#FDF2D5;--warn-fg:#7A5406;
--peri-600:#4C43B0;--peri-500:#6A5FD4;--peri-300:#BAB5F0;--peri-100:#E7E5FB;
--berry-600:#A11F3F;--berry-500:#C22A4E;--berry-100:#FBE0E6;
--font-display:ui-rounded,"SF Pro Rounded","Segoe UI Variable Display",system-ui,-apple-system,"Trebuchet MS",sans-serif;
--font-body:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
--font-mono:ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:20px;--s6:24px;--s7:32px;--s8:40px;--s9:48px;
--r-sm:10px;--r-md:16px;--r-lg:24px;--r-pill:999px;--bw:1.5px;
--hard-sm:2px 2px 0 var(--ink-900);--hard-md:4px 4px 0 var(--ink-900);
--focus:0 0 0 3px var(--peri-300);--dur:140ms;--ease:cubic-bezier(.2,.8,.2,1);
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--cream);color:var(--ink-900);
font:400 16px/1.55 var(--font-body);font-variant-numeric:lining-nums;
text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased}
h1,h2,h3,h4{font-family:var(--font-display);font-weight:700;margin:0;letter-spacing:-.02em}
p{margin:0}
a{color:var(--jade-600);text-decoration-thickness:1.5px;text-underline-offset:2px}
a:hover{color:var(--jade-700)}
img,svg{max-width:100%}
:focus-visible{outline:none;box-shadow:var(--focus);border-radius:var(--r-sm)}
.kt-sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
.kt-skip{position:absolute;left:var(--s4);top:-60px;z-index:50;background:var(--ink-900);color:var(--cream);
padding:10px 18px;border-radius:var(--r-pill);font-weight:600;transition:top var(--dur) var(--ease)}
.kt-skip:focus{top:var(--s3);color:var(--cream)}
.kt-num{font-variant-numeric:tabular-nums lining-nums;font-feature-settings:"tnum" 1,"lnum" 1}
.kt-mono{font-family:var(--font-mono);font-variant-numeric:tabular-nums lining-nums}
.kt-eyebrow{font-family:var(--font-mono);font-size:12px;line-height:1.2;letter-spacing:.14em;
text-transform:uppercase;color:var(--ink-600);font-weight:500}

/* ---------- top bar : the one full-bleed ink section ---------- */
.kt-topbar{background:var(--ink-900);color:var(--cream);border-bottom:3px solid var(--ink-900)}
.kt-topbar--live{border-bottom-color:var(--persimmon-500)}
.kt-topbar-inner{max-width:1280px;margin:0 auto;padding:var(--s4) var(--s5);
display:flex;flex-wrap:wrap;align-items:center;gap:var(--s3) var(--s6)}
.kt-brand{display:flex;align-items:baseline;gap:var(--s3);font-family:var(--font-display);
font-weight:700;font-size:22px;letter-spacing:-.03em}
.kt-brand small{font-family:var(--font-mono);font-size:12px;letter-spacing:.14em;
text-transform:uppercase;color:var(--ink-300);font-weight:500;white-space:nowrap}
.kt-mode{display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-pill);
padding:5px 13px 6px;font-family:var(--font-mono);font-size:12px;letter-spacing:.14em;
text-transform:uppercase;font-weight:500;white-space:nowrap}
.kt-mode--paper{border:var(--bw) solid var(--ink-400);color:var(--ink-100)}
.kt-mode--live{background:var(--persimmon-500);color:var(--ink-900);border:var(--bw) solid var(--persimmon-500);font-weight:700}
.kt-topbar-meta{margin-left:auto;display:flex;flex-wrap:wrap;align-items:center;gap:var(--s3) var(--s6)}
.kt-tb-item{display:flex;flex-direction:column;gap:2px}
.kt-tb-item .kt-eyebrow{color:var(--ink-400);font-size:12px}
.kt-tb-val{font-family:var(--font-mono);font-size:13px;color:var(--cream);
font-variant-numeric:tabular-nums;letter-spacing:.01em}
.kt-refresh{min-width:150px}
.kt-refresh-bar{display:block;height:4px;margin-top:6px;background:var(--ink-700);border-radius:var(--r-pill);overflow:hidden}
.kt-refresh-bar i{display:block;height:100%;width:100%;background:var(--ink-400);border-radius:var(--r-pill)}

/* ---------- page shell ---------- */
.kt-page{max-width:1280px;margin:0 auto;padding:var(--s6) var(--s5) var(--s9)}
@media (min-width:900px){.kt-page{padding:var(--s7) var(--s7) var(--s9)}}
.kt-block{margin-top:var(--s7)}
.kt-block:first-child{margin-top:0}
.kt-block-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--s2) var(--s4);margin-bottom:var(--s4)}
.kt-block-head h2{font-size:24px;flex:0 1 auto}
.kt-block-note{color:var(--ink-600);font-size:14px;max-width:70ch}
.kt-block-head .kt-block-note{margin-left:auto;text-align:right;flex:1 1 240px;min-width:0}
@media (max-width:719px){.kt-block-head .kt-block-note{margin-left:0;text-align:left}}
.kt-card{background:var(--paper);border:var(--bw) solid var(--ink-900);border-radius:var(--r-lg);padding:var(--s5)}
@media (min-width:900px){.kt-card{padding:var(--s6)}}

/* ---------- alert banner ---------- */
.kt-alert{border:var(--bw) solid var(--ink-900);border-radius:var(--r-lg);padding:var(--s5);
display:flex;gap:var(--s4);align-items:flex-start;margin-bottom:var(--s6)}
.kt-alert svg{flex:none;width:28px;height:28px;margin-top:2px}
.kt-alert h2{font-size:22px;margin-bottom:4px}
.kt-alert p{font-size:15px;max-width:62ch}
.kt-alert--stop{background:var(--berry-500);color:var(--paper);border-color:var(--ink-900);box-shadow:var(--hard-md)}
.kt-alert--stop p{color:#FCE9EE}
.kt-alert--freeze{background:var(--butter-100)}
.kt-alert--freeze p{color:var(--warn-fg)}
.kt-alert--live{background:var(--persimmon-500)}
.kt-alert-stack{display:grid;gap:var(--s3);margin-bottom:var(--s6)}
.kt-alert-stack .kt-alert{margin-bottom:0}

/* ---------- flag tiles ---------- */
.kt-flags{display:grid;gap:var(--s3);grid-template-columns:1fr}
@media (min-width:640px){.kt-flags{grid-template-columns:repeat(3,1fr)}}
.kt-flag{border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);padding:var(--s4) var(--s5) var(--s5);
background:var(--paper);display:flex;flex-direction:column;gap:var(--s2);min-height:118px;min-width:0}
.kt-flag-top{display:flex;align-items:center;justify-content:space-between;gap:var(--s3)}
.kt-flag-state{display:flex;align-items:center;gap:var(--s2);font-family:var(--font-display);
font-weight:700;font-size:27px;letter-spacing:-.02em;line-height:1.05}
.kt-flag-state svg{width:22px;height:22px;flex:none}
.kt-flag-why{font-size:13.5px;color:var(--ink-600);margin-top:auto}
.kt-flag--ok{background:var(--jade-100)}
.kt-flag--ok .kt-flag-state{color:var(--jade-700)}
.kt-flag--ok .kt-flag-why{color:var(--jade-700)}
.kt-flag--warn{background:var(--butter-100)}
.kt-flag--warn .kt-flag-state,.kt-flag--warn .kt-flag-why{color:var(--warn-fg)}
.kt-flag--stop{background:var(--berry-500);border-color:var(--ink-900)}
.kt-flag--stop .kt-eyebrow{color:#F6D7DF}
.kt-flag--stop .kt-flag-state{color:var(--paper)}
.kt-flag--stop .kt-flag-why{color:#FBE0E6}

/* ---------- context strip ---------- */
.kt-context{margin-top:var(--s3);border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);
background:var(--paper);padding:var(--s4) var(--s5);display:grid;gap:var(--s4);
grid-template-columns:repeat(auto-fit,minmax(148px,1fr))}
.kt-ctx{display:flex;flex-direction:column;gap:3px}
.kt-ctx-val{font-family:var(--font-mono);font-size:15px;font-variant-numeric:tabular-nums;font-weight:500}
.kt-ctx-sub{font-size:12.5px;color:var(--ink-500)}

/* ---------- badges ---------- */
.kt-badge{display:inline-flex;align-items:center;gap:5px;border-radius:var(--r-pill);
padding:3px 11px 4px;font-family:var(--font-mono);font-size:12px;letter-spacing:.06em;
text-transform:uppercase;font-weight:500;white-space:nowrap;border:1px solid transparent}
.kt-badge svg{width:13px;height:13px;flex:none}
.kt-badge--neutral{background:var(--ink-100);color:var(--ink-700);border-color:var(--ink-200)}
.kt-badge--ok{background:var(--jade-100);color:var(--jade-700);border-color:var(--jade-300,#8FCEBC)}
.kt-badge--warn{background:var(--butter-100);color:var(--warn-fg);border-color:var(--butter-400,#F8C94F)}
.kt-badge--bad{background:var(--berry-100);color:var(--berry-600);border-color:var(--berry-300,#EC9EB0)}
.kt-badge--info{background:var(--peri-100);color:var(--peri-600);border-color:var(--peri-300)}
.kt-badge--ink{background:var(--ink-900);color:var(--cream)}

/* ---------- deltas ---------- */
.kt-delta{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;flex:none;font-variant-numeric:tabular-nums lining-nums;font-weight:600}
.kt-delta svg{width:14px;height:14px;flex:none;stroke-width:2.6}
.kt-delta--up{color:var(--jade-700)}
.kt-delta--down{color:var(--berry-600)}
.kt-delta--flat{color:var(--ink-600)}

/* ---------- stats ---------- */
.kt-stats{display:grid;gap:var(--s3);grid-template-columns:1fr}
@media (min-width:560px){.kt-stats{grid-template-columns:repeat(2,1fr)}}
@media (min-width:980px){.kt-stats{grid-template-columns:repeat(4,1fr)}}
.kt-stat{border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);background:var(--paper);
padding:var(--s4) var(--s5) var(--s5);display:flex;flex-direction:column;gap:var(--s2);min-width:0}
.kt-stat-fig{font-family:var(--font-display);font-weight:700;font-size:30px;line-height:1.02;
letter-spacing:-.03em;font-variant-numeric:tabular-nums lining-nums}
@media (min-width:640px){.kt-stat-fig{font-size:34px}}
.kt-stat-sub{font-size:13.5px;color:var(--ink-600);margin-top:auto}
.kt-stat--pl .kt-stat-fig{display:flex;align-items:center;gap:var(--s2)}
.kt-stat--pl svg{width:24px;height:24px;flex:none;stroke-width:2.6}

/* ---------- chart ---------- */
.kt-chart-card{margin-top:var(--s3)}
.kt-chart-summary{display:flex;flex-wrap:wrap;gap:var(--s3) var(--s7);margin-bottom:var(--s5)}
.kt-chart-summary>div{display:flex;flex-direction:column;gap:3px}
.kt-chart-summary b{font-family:var(--font-mono);font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.kt-plotwrap{position:relative;display:flex;gap:var(--s2)}
.kt-yaxis{flex:none;width:62px;position:relative;height:300px}
.kt-ytick{position:absolute;right:0;transform:translateY(-50%);font-family:var(--font-mono);
font-size:12px;color:var(--ink-500);font-variant-numeric:tabular-nums;white-space:nowrap}
.kt-plot{position:relative;flex:1;min-width:0;height:300px;border:1px solid var(--ink-200);
border-radius:var(--r-sm);background:var(--paper);touch-action:pan-y;cursor:crosshair}
@media (max-width:719px){.kt-plot,.kt-yaxis{height:224px}.kt-yaxis{width:54px}}
.kt-plot svg{display:block;width:100%;height:100%;border-radius:var(--r-sm)}
.kt-plot:focus-visible{box-shadow:var(--focus)}
.kt-gap-label{position:absolute;top:8px;transform:translateX(-50%);font-family:var(--font-mono);
font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-500);
white-space:nowrap;pointer-events:none}
.kt-lastdot{position:absolute;width:11px;height:11px;margin:-5.5px 0 0 -5.5px;border-radius:50%;
background:var(--jade-600);border:2px solid var(--paper);pointer-events:none}
.kt-xaxis{position:relative;height:22px;margin:6px 0 0 70px}
@media (max-width:719px){.kt-xaxis{margin-left:62px}}
.kt-xtick{position:absolute;transform:translateX(-50%);font-family:var(--font-mono);font-size:12px;
color:var(--ink-500);white-space:nowrap}
@media (max-width:620px){.kt-xtick:nth-child(even){display:none}}
.kt-cross{position:absolute;top:0;bottom:0;width:1px;background:var(--ink-700);
pointer-events:none;opacity:0;transform:translateX(-.5px)}
.kt-crossdot{position:absolute;width:13px;height:13px;margin:-6.5px 0 0 -6.5px;border-radius:50%;
background:var(--persimmon-500);border:2px solid var(--ink-900);pointer-events:none;opacity:0}
.kt-tip{position:absolute;z-index:9;pointer-events:none;opacity:0;background:var(--ink-900);
color:var(--cream);border-radius:var(--r-md);padding:9px 13px;min-width:132px;
box-shadow:0 6px 16px -4px rgba(23,20,15,.28);transform:translate(-50%,calc(-100% - 14px))}
.kt-tip{white-space:nowrap}
.kt-tip b{display:block;font-family:var(--font-mono);font-size:16px;font-weight:600;
font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kt-tip span{display:block;font-family:var(--font-mono);font-size:12px;color:var(--ink-300);margin-top:3px}
.kt-cross.is-on,.kt-crossdot.is-on,.kt-tip.is-on{opacity:1}
.kt-tip--left{transform:translate(-100%,calc(-100% - 14px)) translateX(-10px)}
.kt-tip--right{transform:translate(0,calc(-100% - 14px)) translateX(10px)}
.kt-chart-hint{margin-top:var(--s3);font-size:13px;color:var(--ink-500)}
@media (max-width:559px){.kt-gap-label{display:none}}

/* ---------- empty + error ---------- */
.kt-empty{border:var(--bw) dashed var(--ink-300);border-radius:var(--r-md);background:var(--ink-050);
padding:var(--s6) var(--s5);text-align:center}
.kt-empty h3{font-size:19px;margin-bottom:6px}
.kt-empty p{color:var(--ink-600);font-size:14.5px;max-width:52ch;margin:0 auto}
.kt-error{border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);background:var(--butter-100);
padding:var(--s4) var(--s5);display:flex;gap:var(--s3);align-items:flex-start}
.kt-error svg{width:20px;height:20px;flex:none;margin-top:3px;color:var(--warn-fg)}
.kt-error h3{font-size:16px;color:var(--warn-fg);margin-bottom:3px}
.kt-error p{font-size:13.5px;color:var(--warn-fg);max-width:62ch}
.kt-error code{display:block;font-family:var(--font-mono);font-size:12.5px;margin-top:var(--s2);
padding:8px 10px;background:var(--paper);border:1px solid var(--ink-200);border-radius:var(--r-sm);
color:var(--ink-800);overflow-wrap:anywhere}

/* ---------- approvals ---------- */
.kt-approve-card{background:var(--paper);border:var(--bw) solid var(--ink-900);
border-radius:var(--r-lg);padding:var(--s5);box-shadow:var(--hard-md)}
@media (min-width:900px){.kt-approve-card{padding:var(--s6)}}
.kt-approve-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--s2) var(--s4);margin-bottom:var(--s4)}
.kt-approve-head h3{font-size:21px;flex:0 1 auto;min-width:0}
.kt-approve-when{margin-left:auto;font-family:var(--font-mono);font-size:12.5px;color:var(--ink-500)}
.kt-diff{display:grid;gap:var(--s4);margin-bottom:var(--s5)}
@media (min-width:760px){.kt-diff{grid-template-columns:repeat(3,1fr)}}
.kt-diff-group{border:1px solid var(--ink-200);border-radius:var(--r-md);padding:var(--s3) var(--s4) var(--s4)}
.kt-diff-group .kt-eyebrow{display:block;margin-bottom:var(--s3)}
.kt-chips{display:flex;flex-wrap:wrap;gap:var(--s2)}
.kt-chip{display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-pill);
padding:6px 13px 7px;font-size:14px;font-weight:600;border:1px solid transparent}
.kt-chip svg{width:14px;height:14px;flex:none;stroke-width:2.6}
.kt-chip small{font-weight:400;color:inherit;opacity:.75;font-size:12.5px}
.kt-chip--add{background:var(--jade-100);color:var(--jade-700);border-color:#8FCEBC}
.kt-chip--rm{background:var(--berry-100);color:var(--berry-600);border-color:#EC9EB0;text-decoration:line-through}
.kt-chip--rm small{text-decoration:none}
.kt-chip--keep{background:var(--paper);color:var(--ink-700);border-color:var(--ink-300)}
.kt-reason{background:var(--ink-050);border:1px solid var(--ink-200);border-radius:var(--r-md);
padding:var(--s4) var(--s5);margin-bottom:var(--s5)}
.kt-reason p{font-size:15px;line-height:1.6;max-width:62ch;text-wrap:pretty;color:var(--ink-800)}
.kt-reason .kt-eyebrow{display:block;margin-bottom:var(--s2)}
.kt-actions{display:flex;flex-direction:column;gap:var(--s5);border-top:1px solid var(--ink-200);padding-top:var(--s5)}
@media (min-width:600px){.kt-actions{flex-direction:row;align-items:center;justify-content:space-between;gap:var(--s9)}}
.kt-btn{font:600 16px/1 var(--font-body);display:inline-flex;align-items:center;justify-content:center;
gap:var(--s2);min-height:52px;padding:0 var(--s7);border-radius:var(--r-pill);cursor:pointer;
border:var(--bw) solid var(--ink-900);transition:background var(--dur) var(--ease),transform var(--dur) var(--ease);width:100%}
@media (max-width:599px){.kt-btn{padding:0 var(--s4)}}
@media (min-width:600px){.kt-btn{width:auto}}
.kt-btn svg{width:18px;height:18px;stroke-width:2.4}
.kt-btn--primary{background:var(--ink-900);color:var(--cream)}
.kt-btn--primary:hover{background:var(--ink-700)}
.kt-btn--primary:active{transform:scale(.97)}
.kt-btn--ghost{background:var(--paper);color:var(--berry-600);border-color:var(--berry-500)}
.kt-btn--ghost:hover{background:var(--berry-100)}
.kt-btn--ghost:active{transform:scale(.97)}
.kt-btn:focus-visible{box-shadow:var(--focus)}
.kt-actions form{margin:0;display:flex;flex-direction:column;gap:var(--s2)}
@media (min-width:600px){.kt-actions form{align-items:flex-start}}
.kt-btn-hint{font-size:12.5px;color:var(--ink-500);text-align:center}
@media (min-width:600px){.kt-btn-hint{text-align:left;padding-left:var(--s4)}}
.kt-queued{display:flex;gap:var(--s3);align-items:flex-start;background:var(--peri-100);
border:var(--bw) solid var(--peri-500);border-radius:var(--r-md);padding:var(--s4) var(--s5);margin-top:var(--s5)}
.kt-queued svg{width:22px;height:22px;flex:none;margin-top:2px;color:var(--peri-600)}
.kt-queued h4{font-size:16px;color:var(--peri-600);margin-bottom:3px}
.kt-queued p{font-size:14px;color:var(--peri-600);max-width:56ch}

/* ---------- responsive tables ---------- */
.kt-tablewrap{border:var(--bw) solid var(--ink-900);border-radius:var(--r-lg);background:var(--paper);overflow:hidden}
.kt-table-cap{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--s2) var(--s4);
padding:var(--s4) var(--s5);border-bottom:1px solid var(--ink-200)}
.kt-table-cap h3{font-size:18px;flex:0 1 auto}
.kt-table-cap .kt-tot{margin-left:auto;display:flex;gap:var(--s5);flex-wrap:wrap}
.kt-table-cap .kt-tot div{display:flex;flex-direction:column;gap:2px;align-items:flex-end}
.kt-table-cap .kt-tot b{font-family:var(--font-mono);font-size:14px;font-variant-numeric:tabular-nums}
.kt-scroll{overflow-x:auto}
table.kt-t{width:100%;border-collapse:collapse;font-size:14.5px}
table.kt-t th,table.kt-t td{text-align:right;padding:var(--s3);border-bottom:1px solid var(--ink-100);
vertical-align:top;white-space:nowrap}
table.kt-t thead th{font-family:var(--font-mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
color:var(--ink-600);font-weight:500;background:var(--ink-050);border-bottom:1px solid var(--ink-200);position:relative}
table.kt-t tbody tr:last-child td,table.kt-t tbody tr:last-child th{border-bottom:0}
table.kt-t .kt-l{text-align:left}
table.kt-t td.kt-n,table.kt-t th.kt-n{font-family:var(--font-mono);font-variant-numeric:tabular-nums lining-nums}
table.kt-t tbody tr:hover{background:var(--ink-050)}
.kt-inst{display:flex;flex-direction:column;gap:2px;white-space:normal;min-width:0}
.kt-inst-main{font-family:var(--font-display);font-weight:700;font-size:17px;letter-spacing:-.015em}
.kt-inst-sub{font-size:12.5px;color:var(--ink-600)}
.kt-inst-raw{font-family:var(--font-mono);font-size:12.5px;color:var(--ink-500);letter-spacing:.02em}
/* Right-aligns a badge cell. NOT display:flex here: that takes the <td>
   out of the table layout and merges it with the next column. The
   narrow-screen block below turns every cell into a flex row anyway. */
.kt-cell-tag{text-align:right}

@media (max-width:979px){
  .kt-tablewrap{border:0;border-radius:0;background:transparent;overflow:visible}
  .kt-table-cap{border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);background:var(--paper);margin-bottom:var(--s3)}
  .kt-scroll{overflow:visible}
  table.kt-t,table.kt-t tbody,table.kt-t tr,table.kt-t td,table.kt-t th{display:block;width:auto}
  table.kt-t thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
  table.kt-t tr{background:var(--paper);border:var(--bw) solid var(--ink-900);border-radius:var(--r-md);
  padding:var(--s4);margin-bottom:var(--s3)}
  table.kt-t tbody tr:hover{background:var(--paper)}
  table.kt-t td,table.kt-t th{border:0;padding:5px 0;text-align:right;display:flex;
  align-items:baseline;justify-content:space-between;gap:var(--s4);white-space:normal}
  .kt-table-cap{flex-direction:column;align-items:flex-start}
  .kt-table-cap .kt-tot{margin-left:0;width:100%;justify-content:space-between}
  .kt-table-cap .kt-tot div{align-items:flex-start}
  table.kt-t td::before,table.kt-t th[data-label]::before{white-space:nowrap;content:attr(data-label);
  font-family:var(--font-mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-600);font-weight:500;text-align:left;flex:none}
  table.kt-t th.kt-rowhead{display:block;padding:0 0 var(--s3);margin-bottom:var(--s2);
  border-bottom:1px solid var(--ink-100)}
  table.kt-t th.kt-rowhead::before{content:none}
  table.kt-t tbody tr:last-child td,table.kt-t tbody tr:last-child th{border-bottom:0}
  table.kt-t tbody tr:last-child th.kt-rowhead{border-bottom:1px solid var(--ink-100)}
  table.kt-t td.kt-cell-tag{justify-content:flex-end}
  table.kt-t td.kt-cell-tag::before{content:none}
  table.kt-t td.kt-cell-tag .kt-badge{white-space:normal;text-align:right}
}

/* ---------- concentration ---------- */
.kt-conc-scale{display:grid;grid-template-columns:1fr;gap:var(--s2);height:20px;margin:0 0 var(--s2)}
.kt-conc-caprail{position:relative}
.kt-conc-caplabel{position:absolute;left:40%;transform:translateX(-50%);font-family:var(--font-mono);
font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--persimmon-600);white-space:nowrap;font-weight:500}
.kt-conc{list-style:none;margin:0;padding:0;display:grid;gap:var(--s2)}
.kt-conc-row{display:grid;grid-template-columns:1fr;gap:var(--s2);padding:var(--s3) 0;
border-bottom:1px solid var(--ink-100)}
.kt-conc-row:last-child{border-bottom:0}
@media (min-width:760px){
  .kt-conc-row{grid-template-columns:180px 1fr 230px;align-items:center;gap:var(--s4)}
  .kt-conc-scale{grid-template-columns:180px 1fr 230px;gap:var(--s4)}
}
.kt-conc-name{display:flex;align-items:baseline;gap:var(--s2);min-width:0}
.kt-conc-name b{font-family:var(--font-display);font-weight:700;font-size:17px;letter-spacing:-.015em}
.kt-conc-name small{font-size:12.5px;color:var(--ink-600);overflow-wrap:anywhere}
.kt-conc-track{position:relative;height:26px;background:var(--ink-100);border-radius:var(--r-sm);
border:1px solid var(--ink-200);cursor:crosshair}
.kt-conc-track::after{content:"";position:absolute;left:40%;top:0;bottom:0;width:0;
border-left:2px dashed var(--persimmon-500)}
.kt-conc-bar{position:absolute;left:0;top:0;bottom:0;background:var(--jade-500);border-radius:var(--r-sm) 0 0 var(--r-sm)}
.kt-conc-bar--over{background:var(--berry-500)}
.kt-conc-vals{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:var(--s2) var(--s3)}
@media (min-width:760px){.kt-conc-vals{justify-content:flex-end}}
.kt-conc-vals b{font-family:var(--font-mono);font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.kt-conc-vals span{font-family:var(--font-mono);font-size:13px;color:var(--ink-600);
font-variant-numeric:tabular-nums;min-width:52px;text-align:right}
.kt-conc-foot{margin-top:var(--s4);padding-top:var(--s4);border-top:1px solid var(--ink-200);
display:flex;flex-wrap:wrap;gap:var(--s3) var(--s6);align-items:center}
.kt-conc-foot p{font-size:13px;color:var(--ink-600);max-width:74ch}

/* ---------- ai decisions ---------- */
.kt-decs{display:grid;gap:var(--s3)}
.kt-dec{background:var(--paper);border:var(--bw) solid var(--ink-900);border-radius:var(--r-lg);padding:var(--s4) var(--s5) var(--s5)}
.kt-dec--error{background:var(--berry-100);border-style:dashed}
.kt-dec-head{display:flex;flex-wrap:wrap;gap:var(--s3) var(--s4);align-items:flex-start}
.kt-verdict{flex:none;display:inline-flex;align-items:center;gap:6px;border-radius:var(--r-pill);
padding:6px 15px 7px;font-family:var(--font-mono);font-size:12px;letter-spacing:.1em;
text-transform:uppercase;font-weight:600;border:var(--bw) solid var(--ink-900)}
.kt-verdict svg{width:14px;height:14px;stroke-width:2.6}
.kt-verdict--take{background:var(--jade-600);color:var(--paper)}
.kt-verdict--reject{background:var(--ink-100);color:var(--ink-800)}
.kt-verdict--failed{background:var(--berry-500);color:var(--paper)}
.kt-dec-id{flex:1 1 220px;min-width:0}
.kt-dec-when{margin-left:auto;display:flex;flex-direction:column;gap:2px;align-items:flex-end;text-align:right}
.kt-dec-when time{font-family:var(--font-mono);font-size:13px;font-weight:600}
.kt-dec-when small{font-family:var(--font-mono);font-size:12.5px;color:var(--ink-500)}
@media (max-width:520px){.kt-dec-when{margin-left:0;align-items:flex-start;text-align:left}}
.kt-scores{display:grid;gap:var(--s3) var(--s5);margin:var(--s4) 0 0;
grid-template-columns:repeat(auto-fit,minmax(132px,1fr))}
.kt-score{display:flex;flex-direction:column;gap:5px}
.kt-score-val{font-family:var(--font-mono);font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.kt-meter{height:6px;background:var(--ink-100);border-radius:var(--r-pill);overflow:hidden;border:1px solid var(--ink-200)}
.kt-meter i{display:block;height:100%;background:var(--ink-700);border-radius:var(--r-pill)}
.kt-meter i.is-final{background:var(--persimmon-500)}
.kt-tags{display:flex;flex-wrap:wrap;gap:var(--s2);margin-top:var(--s4)}
.kt-thesis{margin-top:var(--s4);border-top:1px solid var(--ink-200);padding-top:var(--s3)}
.kt-thesis>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:var(--s2);
font-family:var(--font-mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;
color:var(--ink-700);font-weight:500;min-height:44px;border-radius:var(--r-sm)}
.kt-thesis>summary::-webkit-details-marker{display:none}
.kt-thesis>summary:hover{color:var(--ink-900)}
.kt-thesis>summary:focus-visible{box-shadow:var(--focus)}
.kt-thesis .kt-chev{width:16px;height:16px;transition:transform 220ms var(--ease)}
.kt-thesis[open] .kt-chev{transform:rotate(180deg)}
.kt-thesis p{font-size:15.5px;line-height:1.62;max-width:62ch;text-wrap:pretty;
color:var(--ink-800);padding-bottom:var(--s3)}
.kt-dec-foot{margin-top:var(--s3);padding-top:var(--s3);border-top:1px solid var(--ink-100);
display:flex;flex-wrap:wrap;gap:var(--s2) var(--s5);font-family:var(--font-mono);
font-size:12.5px;color:var(--ink-500);font-variant-numeric:tabular-nums}
.kt-failbox{margin-top:var(--s4);background:var(--paper);border:1px solid var(--berry-300,#EC9EB0);
border-radius:var(--r-md);padding:var(--s3) var(--s4)}
.kt-failbox .kt-eyebrow{color:var(--berry-600);display:block;margin-bottom:5px}
.kt-failbox code{font-family:var(--font-mono);font-size:13px;color:var(--ink-800);overflow-wrap:anywhere}
.kt-failbox p{font-size:13.5px;color:var(--ink-600);margin-top:6px;max-width:62ch}

/* ---------- footer ---------- */
.kt-foot{margin-top:var(--s9);border-top:var(--bw) solid var(--ink-300);padding-top:var(--s5);
display:grid;gap:var(--s3) var(--s7);grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.kt-foot div{display:flex;flex-direction:column;gap:3px}
.kt-foot span{font-family:var(--font-mono);font-size:12.5px;color:var(--ink-700);font-variant-numeric:tabular-nums}
.kt-foot p{grid-column:1/-1;font-size:13px;color:var(--ink-500);max-width:68ch}

@media (min-width:980px){table.kt-t .kt-inst{max-width:330px}}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{transition-duration:1ms!important;animation-duration:1ms!important}
}
/* ---------- standalone state pages (401 / 503) ---------- */
.kt-state{max-width:640px;margin:0 auto;padding:var(--s9) var(--s5) var(--s9)}
.kt-state h1{font-size:34px;letter-spacing:-.03em;margin-bottom:var(--s3)}
.kt-state>p{font-size:16.5px;line-height:1.6;color:var(--ink-700);max-width:56ch;margin-bottom:var(--s6)}
.kt-varlist{list-style:none;margin:0 0 var(--s5);padding:0;display:grid;gap:var(--s2)}
.kt-varlist li{display:flex;align-items:center;gap:var(--s3);font-family:var(--font-mono);
font-size:14.5px;background:var(--ink-050);border:1px solid var(--ink-200);
border-radius:var(--r-sm);padding:11px 14px;overflow-wrap:anywhere}
.kt-varlist svg{width:16px;height:16px;flex:none;color:var(--berry-600)}
.kt-snippet{font-family:var(--font-mono);font-size:14px;background:var(--ink-900);color:var(--cream);
border-radius:var(--r-md);padding:14px 16px;overflow-wrap:anywhere;margin-bottom:var(--s5)}
.kt-snippet b{color:var(--butter-400,#F8C94F);font-weight:400}
.kt-fine{font-size:12.5px;color:var(--ink-500);max-width:56ch}
"""

#: Inline SVG sprite. Every icon is drawn with <use href="#i-name"/>.
#: The stroke attributes live on each <symbol>, not only on the wrapper:
#: a <use> clone inherits presentation from where it is referenced, not
#: from the sprite, so on the wrapper alone they never reach the paths
#: and every line icon fills solid black.
ICONS = r"""
<svg width="0" height="0" aria-hidden="true" focusable="false" style="position:absolute" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<symbol id="i-up" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></symbol>
<symbol id="i-down" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 5v14M19 12l-7 7-7-7"/></symbol>
<symbol id="i-check" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
<symbol id="i-x" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
<symbol id="i-alert" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4M12 17h.01"/></symbol>
<symbol id="i-stop" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z"/><path d="M15 9l-6 6M9 9l6 6"/></symbol>
<symbol id="i-pause" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M9 4v16M15 4v16"/></symbol>
<symbol id="i-clock" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9.5"/><path d="M12 7v5.2l3.4 1.9"/></symbol>
<symbol id="i-plus" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
<symbol id="i-minus" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M5 12h14"/></symbol>
<symbol id="i-chev" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-open" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M7 17 17 7M8 7h9v9"/></symbol>
<symbol id="i-close" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M17 7 7 17M16 17H7V8"/></symbol>
<symbol id="i-assign" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M12 3v11M7 11l5 5 5-5M4 21h16"/></symbol>
<symbol id="i-refresh" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3.2-6.9"/><path d="M21 3.5v6h-6"/></symbol>
<symbol id="i-cache" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 9 9 9 9 0 0 1-9 9"/><path d="M12 21H3l3-3"/><path d="M12 8v4.5l3 1.7"/></symbol>
<symbol id="i-lock" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect x="4" y="10.5" width="16" height="10.5" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></symbol>
<symbol id="i-flag" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M4 21V4h10l1 2h5v9h-6l-1-2H4"/></symbol>
<symbol id="i-shares" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><rect x="3" y="8" width="18" height="12" rx="2"/><path d="M8 8V6a4 4 0 0 1 8 0v2M3 13h18"/></symbol>
</svg>
"""

#: Progressive enhancement only: refresh countdown, chart and bar tooltips.
#: The page is complete and readable with scripting off.
SCRIPT = r"""
(function(){
  "use strict";

  /* ---------- auto-refresh countdown (progressive enhancement) ---------- */
  var out = document.getElementById("kt-countdown");
  var fill = document.getElementById("kt-refresh-fill");
  if (out) {
    var total = 300, left = total;
    var tick = function(){
      var m = Math.floor(left/60), s = left%60;
      out.textContent = "reloads in " + m + ":" + (s<10?"0":"") + s;
      if (fill) fill.style.width = (left/total*100).toFixed(2) + "%";
      if (left > 0) left--;
    };
    tick();
    setInterval(tick, 1000);
  }

  /* ---------- shared tooltip helpers ---------- */
  function place(tip, host, xPct, yPx){
    tip.classList.remove("kt-tip--left","kt-tip--right");
    tip.style.left = xPct + "%";
    tip.style.top = yPx + "px";
    var hostBox = host.getBoundingClientRect();
    var tipBox = tip.getBoundingClientRect();
    if (tipBox.right > window.innerWidth - 8) tip.classList.add("kt-tip--left");
    else if (tipBox.left < 8) tip.classList.add("kt-tip--right");
    void hostBox;
  }

  /* ---------- equity chart ---------- */
  var plot = document.getElementById("kt-plot");
  var raw = document.getElementById("kt-equity-data");
  if (plot && raw) {
    var series = [];
    try { series = JSON.parse(raw.textContent) || []; } catch(e){ series = []; }
    if (series.length > 1) {
      var cross = document.getElementById("kt-cross");
      var dot = document.getElementById("kt-crossdot");
      var tip = document.getElementById("kt-tip");
      var tipVal = tip.querySelector("b");
      var tipTime = tip.querySelector("span");
      var idx = -1;

      var money = function(v){
        return "$" + v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      };

      var show = function(i){
        if (i < 0 || i >= series.length) return;
        idx = i;
        var p = series[i];
        var h = plot.clientHeight;
        cross.style.left = p.x + "%";
        dot.style.left = p.x + "%";
        dot.style.top = p.y + "%";
        tipVal.textContent = money(p.v);
        tipTime.textContent = p.label + " SGT";
        place(tip, plot, p.x, Math.max(p.y / 100 * h, 54));
        cross.classList.add("is-on");
        dot.classList.add("is-on");
        tip.classList.add("is-on");
      };
      var hide = function(){
        cross.classList.remove("is-on");
        dot.classList.remove("is-on");
        tip.classList.remove("is-on");
      };
      var nearest = function(clientX){
        var box = plot.getBoundingClientRect();
        var pct = (clientX - box.left) / box.width * 100;
        var best = 0, bestD = Infinity;
        for (var i = 0; i < series.length; i++) {
          var d = Math.abs(series[i].x - pct);
          if (d < bestD) { bestD = d; best = i; }
        }
        return best;
      };

      plot.addEventListener("mousemove", function(e){ show(nearest(e.clientX)); });
      plot.addEventListener("mouseleave", hide);
      plot.addEventListener("touchstart", function(e){
        if (e.touches[0]) show(nearest(e.touches[0].clientX));
      }, {passive:true});
      plot.addEventListener("touchmove", function(e){
        if (e.touches[0]) show(nearest(e.touches[0].clientX));
      }, {passive:true});
      plot.addEventListener("keydown", function(e){
        var k = e.key, n = series.length;
        if (k === "ArrowRight" || k === "ArrowUp") { show(idx < 0 ? 0 : Math.min(n-1, idx+1)); e.preventDefault(); }
        else if (k === "ArrowLeft" || k === "ArrowDown") { show(idx < 0 ? n-1 : Math.max(0, idx-1)); e.preventDefault(); }
        else if (k === "Home") { show(0); e.preventDefault(); }
        else if (k === "End") { show(n-1); e.preventDefault(); }
        else if (k === "Escape") { hide(); }
      });
      plot.addEventListener("blur", hide);
      document.addEventListener("click", function(e){
        if (!plot.contains(e.target)) hide();
      });
      window.addEventListener("resize", hide);
    }
  }

  /* ---------- concentration hover readout ---------- */
  var conc = document.getElementById("kt-conc");
  if (conc) {
    var ctip = document.createElement("div");
    ctip.className = "kt-tip";
    ctip.setAttribute("role", "status");
    ctip.innerHTML = "<b></b><span></span>";
    var cv = ctip.querySelector("b"), cs = ctip.querySelector("span");

    var showConc = function(track, clientX){
      var row = track.closest(".kt-conc-row");
      if (!row) return;
      track.style.position = "relative";
      if (ctip.parentNode !== track) track.appendChild(ctip);
      cv.textContent = row.getAttribute("data-usd");
      cs.textContent = row.getAttribute("data-name") + " · " + row.getAttribute("data-pct");
      var box = track.getBoundingClientRect();
      var pct = clientX === null ? 50 : Math.max(0, Math.min(100, (clientX - box.left) / box.width * 100));
      place(ctip, track, pct, 0);
      ctip.classList.add("is-on");
    };
    var hideConc = function(){ ctip.classList.remove("is-on"); };

    conc.addEventListener("mousemove", function(e){
      var t = e.target.closest(".kt-conc-track");
      if (t) showConc(t, e.clientX); else hideConc();
    });
    conc.addEventListener("mouseleave", hideConc);
    conc.addEventListener("focusin", function(e){
      var t = e.target.closest(".kt-conc-track");
      if (t) showConc(t, null);
    });
    conc.addEventListener("focusout", hideConc);
    conc.addEventListener("touchstart", function(e){
      var t = e.target.closest(".kt-conc-track");
      if (t && e.touches[0]) showConc(t, e.touches[0].clientX);
    }, {passive:true});
    document.addEventListener("click", function(e){
      if (!conc.contains(e.target)) hideConc();
    });
  }

  /* ---------- open the two newest theses on wide screens ---------- */
  if (window.matchMedia && window.matchMedia("(min-width: 1024px)").matches) {
    var th = document.querySelectorAll(".kt-decs .kt-thesis");
    for (var j = 0; j < Math.min(2, th.length); j++) th[j].open = true;
  }
})();
"""
