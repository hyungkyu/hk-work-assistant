"""The two digest pages, rendered as standalone HTML by the batch.

Same palette and the same layout as the backoffice, so a page written to disk
and the same page served by the app do not look like two products. The markup
is deliberately plain: no framework, no fetch, no script. A file on disk that
needs a running server to be readable is not a record.

Everything here is a rendering of data the batch already computed. There is
no model in this path and nothing is summarised -- the person page lists every
activity of the day in time order, which is what HK asked for on 2026-09-11
("주요가 아니라 모든 것").
"""

from __future__ import annotations

import html
from typing import Any

STYLE = """
:root{color-scheme:dark;--bg:#0b0f14;--panel:#121821;--panel-2:#171f2b;--line:#293341;
--line-soft:#1e2734;--text:#e8edf4;--muted:#91a0b4;--dim:#6d7b8e;--accent:#65d6a6;
--accent-2:#68a8ff;--warning:#f1c56b;--danger:#ff7b7b;
--ui:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--ui);-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding-block:36px 80px;padding-left:20px;padding-right:20px}
h1{font-size:clamp(22px,3vw,30px);letter-spacing:-.03em;margin:0 0 6px;text-wrap:balance}
h2{font-size:16px;margin:0;letter-spacing:-.01em}
.lead{color:var(--muted);margin:0 0 22px;font-size:13.5px}
.src{font:400 11px/1.5 var(--mono);color:var(--dim)}
.src b{color:#8796aa;font-weight:500}
.card{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:16px;margin-bottom:16px}
.h{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.pill{border:1px solid var(--line);border-radius:999px;padding:3px 9px;font-size:11.5px;color:var(--muted);white-space:nowrap}
.pill.ok{color:var(--accent);border-color:#286b52;background:#102b22}
.pill.warn{color:var(--warning);border-color:#705a2c;background:#2b2415}
.pill.info{color:var(--accent-2);border-color:#2c4d7a;background:#121e2e}
.counts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.count{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:14px 16px}
.count.split{border-left:2px solid var(--accent-2)}
.count .k{font-size:11.5px;color:var(--muted)}
.count .v{font:700 26px/1.1 var(--ui);font-variant-numeric:tabular-nums;margin:7px 0 6px;letter-spacing:-.02em}
.count .n{font:400 11px/1.45 var(--mono);color:var(--dim)}
.tree{display:grid;gap:2px;font-size:13.5px}
.node{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;padding:7px 9px;border-radius:7px;color:var(--muted)}
.node .nm{display:flex;align-items:center;gap:7px;min-width:0}
.node .cnt{font:500 11.5px/1 var(--mono);color:var(--dim);font-variant-numeric:tabular-nums}
.node .dot{width:5px;height:5px;border-radius:50%;background:#3b4859;flex:none}
.node.d0{color:var(--text);font-weight:600;margin-top:10px}
.node.d0 .dot{background:var(--accent)}
.node.d0:first-child{margin-top:0}
.people{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-top:10px}
.person{border:1px solid var(--line);background:var(--panel-2);border-radius:10px;padding:12px;display:grid;gap:7px;
text-decoration:none;color:inherit}
.person:hover{border-color:#3a4a5e}
.person .top{display:flex;align-items:center;gap:9px}
.av{width:30px;height:30px;border-radius:8px;background:var(--panel);border:1px solid var(--line);
display:grid;place-items:center;font:600 12px/1 var(--ui);color:var(--muted);flex:none}
.av.lg{width:52px;height:52px;border-radius:12px;font-size:18px}
.person strong{display:block;font-size:13.5px;font-weight:600}
.person small{display:block;color:var(--dim);font-size:11.5px;margin-top:2px}
.tags{display:flex;gap:5px;flex-wrap:wrap}
.tags .pill{font-size:10.5px;padding:2px 7px}
.grp{border:1px solid var(--line-soft);border-radius:10px;padding:12px;margin-top:12px;background:#0e131a}
.grp>.h{margin-bottom:4px}
.grp h3{margin:0;font-size:14px}
.phead{display:grid;grid-template-columns:auto 1fr;gap:16px;align-items:start}
.phead h2{font-size:20px}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:16px}
.stat{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:11px 12px}
.stat .k{font-size:11px;color:var(--muted)}
.stat .v{font:700 20px/1.15 var(--ui);font-variant-numeric:tabular-nums;margin-top:6px}
.ev{display:grid;grid-template-columns:56px 1fr;gap:12px;padding:10px 0;border-bottom:1px solid var(--line-soft)}
.ev:last-child{border-bottom:0}
.ev .t{font:400 12px/1.6 var(--mono);color:var(--dim);font-variant-numeric:tabular-nums}
.ev .c{min-width:0;display:grid;gap:4px}
.ev .l1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px}
.ev .where{color:var(--dim);font:400 11.5px/1.4 var(--mono);overflow-wrap:anywhere}
.ev a{color:var(--accent-2);text-decoration:none}
.ev a:hover{text-decoration:underline}
.tag{font:500 10.5px/1 var(--mono);letter-spacing:.04em;text-transform:uppercase;border-radius:4px;
padding:4px 6px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag.slack{color:#d8b4fe;border-color:#4a3568;background:#1e152b}
.tag.notion{color:#cbd5e1;border-color:#3a4553;background:#181f29}
.tag.github{color:var(--accent-2);border-color:#2c4d7a;background:#121e2e}
.tag.slurm{color:var(--accent);border-color:#286b52;background:#102b22}
.tag.google_calendar{color:var(--warning);border-color:#705a2c;background:#2b2415}
.empty{border:1px dashed var(--line);border-radius:10px;padding:16px;color:var(--muted);font-size:13px}
.notice{border-left:2px solid var(--warning);background:#1d1a12;padding:12px 14px;border-radius:0 8px 8px 0;
color:#e6d9b4;font-size:13px;line-height:1.55;margin-bottom:16px}
@media(max-width:640px){.phead{grid-template-columns:1fr}}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{_e(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f"<div class=\"wrap\">\n{body}\n</div>\n</body>\n</html>\n"
    )


def _initial(name: str) -> str:
    return _e((name or "?").strip()[:1] or "?")


def _person_card(person: dict, *, link: str | None = None) -> str:
    tags = [f'<span class="pill">{_e(person.get("affiliation"))}</span>']
    access = person.get("access_level")
    if access == "staff_equivalent":
        tags.append('<span class="pill ok">staff</span>')
    elif access:
        tags.append(f'<span class="pill">{_e(access)}</span>')
    if person.get("status") == "absent_from_sheet":
        tags.append('<span class="pill warn">시트에 없음</span>')
    subtitle = " · ".join(
        part for part in (person.get("nickname"), person.get("employment_type") or person.get("title")) if part
    )
    tag = "a" if link else "div"
    href = f' href="{_e(link)}"' if link else ""
    return (
        f'<{tag} class="person"{href}>'
        f'<div class="top"><div class="av">{_initial(person.get("name"))}</div>'
        f'<div><strong>{_e(person.get("name"))}</strong>'
        f'<small>{_e(subtitle) or "&nbsp;"}</small></div></div>'
        f'<div class="tags">{"".join(tags)}</div>'
        f"</{tag}>"
    )


def _tree_rows(nodes: list[dict], out: list[str]) -> None:
    for node in nodes:
        depth = min(node["depth"], 4)
        indent = f"padding-left:{9 + depth * 16}px" if depth else ""
        out.append(
            f'<div class="node d{depth}" style="{indent}">'
            f'<div class="nm"><i class="dot"></i><span>{_e(node["name"])}</span></div>'
            f'<div class="cnt">{node["people"]}</div></div>'
        )
        _tree_rows(node["children"], out)


def _group_sections(nodes: list[dict], out: list[str], person_link=None) -> None:
    """Every node that has members of its own, as a titled group of cards."""
    for node in nodes:
        if node["members"]:
            cards = "".join(
                _person_card(person, link=person_link(person) if person_link else None)
                for person in node["members"]
            )
            out.append(
                f'<div class="grp"><div class="h"><h3>{_e(node["path"])}</h3>'
                f'<span class="pill">{len(node["members"])}명</span></div>'
                f'<div class="people">{cards}</div></div>'
            )
        _group_sections(node["children"], out, person_link)


def render_org_chart(chart: dict[str, Any], *, person_link=None) -> str:
    """The org chart page, from `org.chart.org_chart`."""
    counts = chart.get("headcount") or {}
    by_access = counts.get("by_access") or {}
    by_affiliation = counts.get("by_affiliation") or {}
    by_status = counts.get("by_status") or {}
    access_total = sum(by_access.values())

    head = [
        "<h1>조직도</h1>",
        f'<p class="lead">로스터 관측 {_e(chart.get("observed_at") or "없음")} 기준 · '
        "배치가 만든 화면이고, 여기서는 아무것도 고칠 수 없다.</p>",
    ]
    if chart.get("reason"):
        head.append(f'<div class="notice">{_e(chart["reason"])}</div>')

    tiles = [
        '<div class="count"><div class="k">고유 인원</div>'
        f'<div class="v">{counts.get("people", 0)}</div>'
        '<div class="n">사람 수. 방문연구원은 한 번</div></div>',
        '<div class="count split"><div class="k">접근 기준 합계</div>'
        f'<div class="v">{access_total}</div>'
        f'<div class="n">{_e(" · ".join(f"{k} {v}" for k, v in by_access.items()))}</div></div>',
        '<div class="count"><div class="k">소속 구분</div>'
        f'<div class="v">{len(by_affiliation)}</div>'
        f'<div class="n">{_e(" · ".join(f"{k} {v}" for k, v in by_affiliation.items()))}</div></div>',
    ]
    absent = by_status.get("absent_from_sheet", 0)
    tiles.append(
        '<div class="count"><div class="k">시트에서 사라진 인원</div>'
        f'<div class="v">{absent}</div>'
        "<div class=\"n\">행은 지우지 않는다 — 상태만 바꾼다</div></div>"
    )
    unmapped = chart.get("unmapped_accounts") or []
    if unmapped:
        listed = " · ".join(f'{item["kind"]} {item["value"]}' for item in unmapped[:4])
        tiles.append(
            '<div class="count"><div class="k">주인 없는 계정</div>'
            f'<div class="v" style="color:var(--warning)">{len(unmapped)}</div>'
            f'<div class="n">{_e(listed)}</div></div>'
        )

    tree_rows: list[str] = []
    _tree_rows(chart.get("tree") or [], tree_rows)
    groups: list[str] = []
    _group_sections(chart.get("tree") or [], groups, person_link)

    body = "\n".join(
        [
            *head,
            f'<div class="counts">{"".join(tiles)}</div>',
            '<div class="card"><div class="h"><h2>팀</h2>'
            f'<span class="pill">{len(chart.get("tree") or [])} 루트</span></div>'
            f'<div class="tree">{"".join(tree_rows)}</div>'
            '<div class="src" style="margin-top:12px"><b>org_team</b> · 로스터의 조직 문자열을 '
            "| 로 쪼갠 것. Virtual Lab 은 별도 루트로 다시 세운다</div></div>",
            '<div class="card"><div class="h"><h2>사람</h2>'
            f'<span class="pill">{counts.get("people", 0)}명</span></div>'
            f'{"".join(groups)}'
            '<div class="src" style="margin-top:12px"><b>org_person_state</b> · 최신 관측</div></div>',
        ]
    )
    return _page("조직도", body)


def render_person_day(digest: dict[str, Any]) -> str:
    """One person's day, every activity, in time order."""
    state = digest.get("state") or {}
    identities = digest.get("identities") or []
    counts = (digest.get("counts") or {}).get("by_source") or {}
    events = digest.get("events") or []

    tags = []
    for key in ("affiliation", "access_level", "employment_type"):
        if state.get(key):
            tags.append(f'<span class="pill">{_e(state[key])}</span>')
    for identity in identities:
        tags.append(f'<span class="pill">{_e(identity["kind"])} {_e(identity["value"])}</span>')

    subtitle = " · ".join(
        part
        for part in (state.get("nickname"), state.get("title"), state.get("department_raw"))
        if part
    )

    stats = "".join(
        f'<div class="stat"><div class="k">{_e(source)}</div>'
        f'<div class="v">{count}</div></div>'
        for source, count in counts.items()
    )
    stats += (
        '<div class="stat"><div class="k">합계</div>'
        f'<div class="v">{digest.get("events_total", 0)}</div></div>'
    )

    rows = []
    for event in events:
        where = " · ".join(
            part for part in (event.get("container"), event.get("thread")) if part
        )
        title = event.get("title")
        label = (
            f"<strong>{_e(title)}</strong>"
            if title
            else f'<span class="ev-none" style="color:var(--dim)">제목 없음</span>'
        )
        link = (
            f' <a href="{_e(event["permalink"])}">열기</a>' if event.get("permalink") else ""
        )
        rows.append(
            f'<div class="ev"><div class="t">{_e(event.get("time"))}</div><div class="c">'
            f'<div class="l1"><span class="tag {_e(event.get("source"))}">'
            f'{_e(event.get("event_type"))}</span>{label}{link}</div>'
            f'<div class="where">{_e(where)}</div></div></div>'
        )

    day_block = (
        f'<div class="card"><div class="h"><h2>그날 한 일</h2>'
        f'<span class="pill">{len(events)}건 · 시간 순 · 전부</span></div>'
        f'{"".join(rows)}'
        '<div class="src" style="margin-top:12px"><b>timeline_events</b> → '
        "<b>person_day_digest</b> · 기계적으로 나열한다. 요약하지 않고 고르지 않는다</div></div>"
        if rows
        else '<div class="empty">이 날은 수집된 활동이 없다. '
        "<b>활동 없음</b>과 <b>수집 실패</b>는 다른 답이고, 이것은 전자다.</div>"
    )

    truncated = ""
    if digest.get("truncated_at"):
        truncated = (
            f'<div class="notice">이 날은 {digest["truncated_at"]}건이라 '
            "일부만 저장했다. 남은 건수는 원장에 그대로 있다.</div>"
        )

    body = "\n".join(
        [
            f'<h1>{_e(digest.get("name"))}</h1>',
            f'<p class="lead">{_e(digest.get("day"))} (KST) · '
            f'{_e(digest.get("generated_at"))} 에 {_e(digest.get("generator"))} 가 생성</p>',
            truncated,
            '<div class="card"><div class="phead">'
            f'<div class="av lg">{_initial(digest.get("name"))}</div>'
            f'<div><h2>{_e(digest.get("name"))}</h2>'
            f'<div class="sub">{_e(subtitle)}</div>'
            f'<div class="tags" style="margin-top:9px">{"".join(tags)}</div></div></div>'
            '<div class="src" style="margin-top:14px"><b>org_person</b> + '
            "<b>org_person_state</b>(최신 관측) + <b>org_identity</b></div></div>",
            f'<div class="strip">{stats}</div>',
            day_block,
        ]
    )
    return _page(f'{digest.get("name")} · {digest.get("day")}', body)
