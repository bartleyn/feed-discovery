"""
GET /status — human-readable observability page.

Shows current bandit arm rankings, recent impressions, and system summary.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from store import get_db
from store.models import ArmState, Feed, Impression

router = APIRouter(tags=["status"])


def _html(body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Feed Discovery — Status</title>
  <style>
    body {{ font-family: monospace; max-width: 960px; margin: 40px auto; padding: 0 20px; background: #0f0f0f; color: #e0e0e0; }}
    h1 {{ color: #7eb8f7; margin-bottom: 4px; }}
    h2 {{ color: #a0c8a0; margin-top: 32px; border-bottom: 1px solid #333; padding-bottom: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ text-align: left; color: #888; padding: 4px 8px; border-bottom: 1px solid #333; }}
    td {{ padding: 4px 8px; border-bottom: 1px solid #222; }}
    .bar-bg {{ background: #1a1a1a; border-radius: 2px; height: 10px; width: 120px; display: inline-block; vertical-align: middle; }}
    .bar {{ background: #7eb8f7; border-radius: 2px; height: 10px; display: block; }}
    .dim {{ color: #555; }}
    .pending {{ color: #888; font-style: italic; }}
    .reward-hi {{ color: #7eb87e; }}
    .reward-lo {{ color: #b87e7e; }}
    .summary {{ display: flex; gap: 32px; margin: 16px 0; }}
    .stat {{ background: #1a1a1a; padding: 12px 20px; border-radius: 4px; }}
    .stat-val {{ font-size: 24px; color: #7eb8f7; }}
    .stat-lbl {{ font-size: 11px; color: #666; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


@router.get("/status", response_class=HTMLResponse)
def status(db: Session = Depends(get_db)):
    # --- Summary stats ---
    total_feeds = db.query(func.count(Feed.feed_uri)).scalar()
    total_impressions = db.query(func.count(Impression.id)).scalar()
    rewarded = db.query(func.count(Impression.id)).filter(Impression.reward.isnot(None)).scalar()
    pending = total_impressions - rewarded
    avg_reward = db.query(func.avg(Impression.reward)).filter(Impression.reward.isnot(None)).scalar()

    summary = f"""
    <h1>Feed Discovery</h1>
    <div class="summary">
      <div class="stat"><div class="stat-val">{total_feeds}</div><div class="stat-lbl">feeds in registry</div></div>
      <div class="stat"><div class="stat-val">{total_impressions}</div><div class="stat-lbl">total impressions</div></div>
      <div class="stat"><div class="stat-val">{rewarded}</div><div class="stat-lbl">rewarded</div></div>
      <div class="stat"><div class="stat-val">{pending}</div><div class="stat-lbl">pending reward</div></div>
      <div class="stat"><div class="stat-val">{f"{avg_reward:.3f}" if avg_reward else "—"}</div><div class="stat-lbl">avg reward</div></div>
    </div>"""

    # --- Arm rankings ---
    arms = (
        db.query(ArmState, Feed.display_name)
        .join(Feed, ArmState.feed_uri == Feed.feed_uri)
        .filter(ArmState.pulls > 0)
        .all()
    )

    # Sort by posterior mean descending
    arms_sorted = sorted(arms, key=lambda r: r[0].alpha / (r[0].alpha + r[0].beta), reverse=True)
    unplayed = (
        db.query(ArmState, Feed.display_name)
        .join(Feed, ArmState.feed_uri == Feed.feed_uri)
        .filter(ArmState.pulls == 0)
        .count()
    )

    rows = ""
    for arm, display_name in arms_sorted:
        mean = arm.alpha / (arm.alpha + arm.beta)
        bar_width = int(mean * 120)
        rows += f"""<tr>
          <td>{display_name}</td>
          <td>{arm.pulls}</td>
          <td>
            <span class="bar-bg"><span class="bar" style="width:{bar_width}px"></span></span>
            {mean:.3f}
          </td>
          <td class="dim">α={arm.alpha:.1f} β={arm.beta:.1f}</td>
        </tr>"""

    arms_section = f"""
    <h2>Arm Rankings — played ({len(arms_sorted)} feeds, {unplayed} unplayed)</h2>
    <table>
      <tr><th>Feed</th><th>Pulls</th><th>Mean estimate</th><th>Parameters</th></tr>
      {rows if rows else '<tr><td colspan="4" class="dim">No played arms yet.</td></tr>'}
    </table>"""

    # --- Recent impressions ---
    recent = (
        db.query(Impression, Feed.display_name)
        .join(Feed, Impression.feed_uri == Feed.feed_uri)
        .order_by(Impression.shown_at.desc())
        .limit(40)
        .all()
    )

    imp_rows = ""
    for imp, display_name in recent:
        if imp.reward is None:
            reward_cell = '<span class="pending">pending</span>'
        elif imp.reward > 0:
            reward_cell = f'<span class="reward-hi">{imp.reward:.3f}</span>'
        else:
            reward_cell = f'<span class="reward-lo">0.000</span>'

        imp_rows += f"""<tr>
          <td>{imp.shown_at.strftime("%m-%d %H:%M")}</td>
          <td>{display_name}</td>
          <td>{imp.posts_shown}</td>
          <td>{reward_cell}</td>
        </tr>"""

    impressions_section = f"""
    <h2>Recent Impressions</h2>
    <table>
      <tr><th>Shown at</th><th>Feed</th><th>Posts</th><th>Reward</th></tr>
      {imp_rows if imp_rows else '<tr><td colspan="4" class="dim">No impressions yet.</td></tr>'}
    </table>"""

    return HTMLResponse(_html(summary + arms_section + impressions_section))
