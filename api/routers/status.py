"""
GET /status — human-readable observability page (password-protected).

Global view: per-user summary table, system totals, feed registry with
inline priority_boost editor.
Per-user view (?user=<did>): arm rankings, feed performance, recent impressions.

Set STATUS_PASSWORD in .env to require HTTP Basic Auth.
"""

import base64
import secrets

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.config import settings
from store import get_db
from store.models import ArmState, Feed, Impression

router = APIRouter(tags=["status"])


def _check_auth(request: Request) -> bool:
    """Return True if request passes Basic Auth (or no password is configured)."""
    if not settings.status_password:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        _, _, password = decoded.partition(":")
        return secrets.compare_digest(password, settings.status_password)
    except Exception:
        return False


def _auth_required() -> Response:
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Feed Discovery"'},
    )

_CSS = """
body { font-family: monospace; max-width: 1100px; margin: 40px auto; padding: 0 20px; background: #0f0f0f; color: #e0e0e0; }
h1 { color: #7eb8f7; margin-bottom: 4px; }
h2 { color: #a0c8a0; margin-top: 32px; border-bottom: 1px solid #333; padding-bottom: 4px; }
a { color: #7eb8f7; text-decoration: none; }
a:hover { text-decoration: underline; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #888; padding: 4px 8px; border-bottom: 1px solid #333; }
td { padding: 4px 8px; border-bottom: 1px solid #222; vertical-align: middle; }
.bar-bg { background: #1a1a1a; border-radius: 2px; height: 10px; width: 120px; display: inline-block; vertical-align: middle; }
.bar { background: #7eb8f7; border-radius: 2px; height: 10px; display: block; }
.dim { color: #555; }
.pending { color: #888; font-style: italic; }
.reward-hi { color: #7eb87e; }
.reward-lo { color: #b87e7e; }
.summary { display: flex; gap: 32px; margin: 16px 0; flex-wrap: wrap; }
.stat { background: #1a1a1a; padding: 12px 20px; border-radius: 4px; }
.stat-val { font-size: 24px; color: #7eb8f7; }
.stat-lbl { font-size: 11px; color: #666; }
.back { font-size: 12px; color: #555; margin-bottom: 20px; }
.boost-form { display: flex; align-items: center; gap: 4px; }
.boost-form input[type=number] { width: 54px; background: #1a1a1a; border: 1px solid #444; color: #e0e0e0;
  padding: 2px 4px; border-radius: 2px; font-family: monospace; font-size: 12px; }
.boost-form button { background: #2a3a4a; border: 1px solid #4a6a8a; color: #7eb8f7;
  padding: 2px 8px; border-radius: 2px; cursor: pointer; font-size: 11px; }
.boost-form button:hover { background: #3a4a5a; }
.boost-zero { color: #b87e7e; }
.boost-low  { color: #c8a050; }
.boost-full { color: #7eb87e; }
"""


def _html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


def _reward_cell(reward) -> str:
    if reward is None:
        return '<span class="pending">pending</span>'
    if reward > 0:
        return f'<span class="reward-hi">{reward:.3f}</span>'
    return '<span class="reward-lo">0.000</span>'


def _short_did(did: str) -> str:
    """Show first 12 and last 8 chars of a DID for readability."""
    if len(did) <= 24:
        return did
    return f"{did[:12]}…{did[-8:]}"


@router.get("/status", response_class=HTMLResponse)
def status(
    request: Request,
    user: str | None = Query(None, description="Filter to a specific user DID"),
    db: Session = Depends(get_db),
):
    if not _check_auth(request):
        return _auth_required()
    if user:
        return _user_view(user, db)
    return _global_view(db)


@router.post("/status/boost")
def set_boost(
    request: Request,
    feed_uri: str = Form(...),
    priority_boost: float = Form(...),
    db: Session = Depends(get_db),
):
    if not _check_auth(request):
        return _auth_required()
    feed = db.query(Feed).filter(Feed.feed_uri == feed_uri).first()
    if feed:
        feed.priority_boost = max(0.0, min(1.0, priority_boost))
        db.commit()
    return RedirectResponse(url="/status", status_code=303)


# ---------------------------------------------------------------------------
# Global view
# ---------------------------------------------------------------------------

def _global_view(db: Session) -> HTMLResponse:
    total_feeds = db.query(func.count(Feed.feed_uri)).scalar()
    total_impressions = db.query(func.count(Impression.id)).scalar()
    rewarded = db.query(func.count(Impression.id)).filter(Impression.reward.isnot(None)).scalar()
    pending = total_impressions - rewarded
    avg_reward = db.query(func.avg(Impression.reward)).filter(Impression.reward.isnot(None)).scalar()
    distinct_users = db.query(func.count(func.distinct(Impression.user_did))).scalar()

    summary = f"""
    <h1>Feed Discovery</h1>
    <div class="summary">
      <div class="stat"><div class="stat-val">{distinct_users}</div><div class="stat-lbl">users</div></div>
      <div class="stat"><div class="stat-val">{total_feeds}</div><div class="stat-lbl">feeds in registry</div></div>
      <div class="stat"><div class="stat-val">{total_impressions}</div><div class="stat-lbl">total impressions</div></div>
      <div class="stat"><div class="stat-val">{rewarded}</div><div class="stat-lbl">rewarded</div></div>
      <div class="stat"><div class="stat-val">{pending}</div><div class="stat-lbl">pending reward</div></div>
      <div class="stat"><div class="stat-val">{f"{avg_reward:.3f}" if avg_reward else "—"}</div><div class="stat-lbl">avg reward (global)</div></div>
    </div>"""

    # Per-user summary
    user_rows_q = (
        db.query(
            Impression.user_did,
            func.count(Impression.id).label("impressions"),
            func.avg(Impression.reward).label("avg_reward"),
            func.max(Impression.shown_at).label("last_seen"),
        )
        .group_by(Impression.user_did)
        .order_by(func.max(Impression.shown_at).desc())
        .all()
    )

    u_rows = ""
    for row in user_rows_q:
        avg = f"{row.avg_reward:.3f}" if row.avg_reward is not None else "—"
        last = row.last_seen.strftime("%m-%d %H:%M") if row.last_seen else "—"
        short = _short_did(row.user_did)
        u_rows += f"""<tr>
          <td><a href="/status?user={row.user_did}">{short}</a></td>
          <td>{row.impressions}</td>
          <td>{avg}</td>
          <td>{last}</td>
        </tr>"""

    users_section = f"""
    <h2>Users</h2>
    <table>
      <tr><th>User DID</th><th>Impressions</th><th>Avg reward</th><th>Last seen</th></tr>
      {u_rows if u_rows else '<tr><td colspan="4" class="dim">No impressions yet.</td></tr>'}
    </table>"""

    # Feed registry
    feeds = db.query(Feed).order_by(Feed.display_name).all()
    feed_rows = ""
    for f in feeds:
        section = f'<span class="reward-hi">✓</span>' if f.section_post_uri else '<span class="dim">—</span>'
        failures = f.consecutive_failures or 0
        threshold = settings.feed_health_failure_threshold
        if f.last_checked_at is None:
            health_cell = '<span class="dim">unchecked</span>'
        elif failures == 0:
            health_cell = '<span class="reward-hi">ok</span>'
        elif failures < threshold:
            health_cell = f'<span style="color:#c8a050">{failures} failure{"s" if failures != 1 else ""}</span>'
        else:
            health_cell = f'<span class="reward-lo">unhealthy ({failures})</span>'
        checked = f.last_checked_at.strftime("%m-%d %H:%M") if f.last_checked_at else "—"
        boost = f.priority_boost if f.priority_boost is not None else 1.0
        if boost == 0.0:
            boost_cls = "boost-zero"
        elif boost < 1.0:
            boost_cls = "boost-low"
        else:
            boost_cls = "boost-full"
        uri_escaped = f.feed_uri.replace('"', "&quot;")
        boost_form = f"""<form class="boost-form" method="POST" action="/status/boost">
          <input type="hidden" name="feed_uri" value="{uri_escaped}">
          <input type="number" name="priority_boost" value="{boost:.2f}" min="0" max="1" step="0.1">
          <button type="submit">set</button>
        </form>"""
        # Extract creator DID from feed URI (at://did:.../app.bsky.feed.generator/...)
        uri_parts = f.feed_uri.split("/")
        creator_did = uri_parts[2] if len(uri_parts) > 2 else ""
        did_short = creator_did.split(":")[-1][:8] if creator_did else ""
        added = f.added_at.strftime("%Y-%m-%d") if f.added_at else ""
        name_cell = f"""{f.display_name}<br>
          <span class="dim" style="font-size:10px">{did_short} · {added}</span>"""
        feed_rows += f"""<tr>
          <td>{name_cell}</td>
          <td style="text-align:center">{section}</td>
          <td class="{boost_cls}">{boost_form}</td>
          <td>{health_cell}</td>
          <td class="dim">{checked}</td>
        </tr>"""

    feeds_section = f"""
    <h2>Feed Registry ({total_feeds})</h2>
    <table>
      <tr><th>Name</th><th>Section post</th><th>Priority boost</th><th>Health</th><th>Last checked</th></tr>
      {feed_rows if feed_rows else '<tr><td colspan="5" class="dim">No feeds.</td></tr>'}
    </table>"""

    return HTMLResponse(_html("Feed Discovery — Status", summary + users_section + feeds_section))


# ---------------------------------------------------------------------------
# Per-user view
# ---------------------------------------------------------------------------

def _user_view(user_did: str, db: Session) -> HTMLResponse:
    short = _short_did(user_did)

    # Summary stats for this user
    total = db.query(func.count(Impression.id)).filter(Impression.user_did == user_did).scalar()
    rewarded = db.query(func.count(Impression.id)).filter(
        Impression.user_did == user_did, Impression.reward.isnot(None)
    ).scalar()
    pending = total - rewarded
    avg_reward = db.query(func.avg(Impression.reward)).filter(
        Impression.user_did == user_did, Impression.reward.isnot(None)
    ).scalar()
    last_seen = db.query(func.max(Impression.shown_at)).filter(
        Impression.user_did == user_did
    ).scalar()

    summary = f"""
    <p class="back"><a href="/status">← All users</a></p>
    <h1>User: {short}</h1>
    <p class="dim" style="font-size:11px; word-break:break-all">{user_did}</p>
    <div class="summary">
      <div class="stat"><div class="stat-val">{total}</div><div class="stat-lbl">total impressions</div></div>
      <div class="stat"><div class="stat-val">{rewarded}</div><div class="stat-lbl">rewarded</div></div>
      <div class="stat"><div class="stat-val">{pending}</div><div class="stat-lbl">pending reward</div></div>
      <div class="stat"><div class="stat-val">{f"{avg_reward:.3f}" if avg_reward else "—"}</div><div class="stat-lbl">avg reward</div></div>
      <div class="stat"><div class="stat-val">{last_seen.strftime("%m-%d %H:%M") if last_seen else "—"}</div><div class="stat-lbl">last seen</div></div>
    </div>"""

    # Arm rankings for this user
    arms = (
        db.query(ArmState, Feed.display_name)
        .join(Feed, ArmState.feed_uri == Feed.feed_uri)
        .filter(ArmState.user_did == user_did, ArmState.pulls > 0)
        .all()
    )
    arms_sorted = sorted(arms, key=lambda r: r[0].alpha / (r[0].alpha + r[0].beta), reverse=True)
    unplayed = (
        db.query(ArmState)
        .filter(ArmState.user_did == user_did, ArmState.pulls == 0)
        .count()
    )

    arm_rows = ""
    for arm, display_name in arms_sorted:
        mean = arm.alpha / (arm.alpha + arm.beta)
        bar_width = int(mean * 120)
        arm_rows += f"""<tr>
          <td>{display_name}</td>
          <td>{arm.pulls}</td>
          <td>
            <span class="bar-bg"><span class="bar" style="width:{bar_width}px"></span></span>
            {mean:.3f}
          </td>
          <td class="dim">α={arm.alpha:.1f} β={arm.beta:.1f}</td>
        </tr>"""

    arms_section = f"""
    <h2>Arm Rankings — {len(arms_sorted)} played, {unplayed} unplayed</h2>
    <table>
      <tr><th>Feed</th><th>Pulls</th><th>Mean estimate</th><th>Parameters</th></tr>
      {arm_rows if arm_rows else '<tr><td colspan="4" class="dim">No played arms yet.</td></tr>'}
    </table>"""

    # Feed performance: per-feed impression stats for this user
    perf = (
        db.query(
            Feed.display_name,
            func.count(Impression.id).label("impressions"),
            func.avg(Impression.posts_shown).label("avg_posts"),
            func.avg(Impression.reward).label("avg_reward"),
            func.max(Impression.shown_at).label("last_shown"),
        )
        .join(Impression, Impression.feed_uri == Feed.feed_uri)
        .filter(Impression.user_did == user_did)
        .group_by(Feed.feed_uri, Feed.display_name)
        .order_by(func.count(Impression.id).desc())
        .all()
    )

    perf_rows = ""
    for row in perf:
        avg_r = f"{row.avg_reward:.3f}" if row.avg_reward is not None else "—"
        avg_p = f"{row.avg_posts:.1f}" if row.avg_posts is not None else "—"
        last = row.last_shown.strftime("%m-%d %H:%M") if row.last_shown else "—"
        r_cls = "reward-hi" if row.avg_reward and row.avg_reward > 0 else ("reward-lo" if row.avg_reward == 0 else "")
        perf_rows += f"""<tr>
          <td>{row.display_name}</td>
          <td>{row.impressions}</td>
          <td>{avg_p}</td>
          <td class="{r_cls}">{avg_r}</td>
          <td class="dim">{last}</td>
        </tr>"""

    perf_section = f"""
    <h2>Feed Performance</h2>
    <table>
      <tr><th>Feed</th><th>Impressions</th><th>Avg posts/chunk</th><th>Avg reward</th><th>Last shown</th></tr>
      {perf_rows if perf_rows else '<tr><td colspan="5" class="dim">No data.</td></tr>'}
    </table>"""

    # Recent impressions
    recent = (
        db.query(Impression, Feed.display_name)
        .join(Feed, Impression.feed_uri == Feed.feed_uri)
        .filter(Impression.user_did == user_did)
        .order_by(Impression.shown_at.desc())
        .limit(50)
        .all()
    )

    imp_rows = ""
    for imp, display_name in recent:
        imp_rows += f"""<tr>
          <td>{imp.shown_at.strftime("%m-%d %H:%M")}</td>
          <td>{display_name}</td>
          <td>{imp.posts_shown}</td>
          <td>{_reward_cell(imp.reward)}</td>
        </tr>"""

    impressions_section = f"""
    <h2>Recent Impressions</h2>
    <table>
      <tr><th>Shown at</th><th>Feed</th><th>Posts</th><th>Reward</th></tr>
      {imp_rows if imp_rows else '<tr><td colspan="4" class="dim">No impressions yet.</td></tr>'}
    </table>"""

    return HTMLResponse(_html(
        f"Feed Discovery — {short}",
        summary + arms_section + perf_section + impressions_section,
    ))
