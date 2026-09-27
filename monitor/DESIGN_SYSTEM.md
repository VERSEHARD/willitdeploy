# PricePulse interface system

This file is the UI contract for PricePulse. Future UI work should preserve these rules.

## Product sentence
PricePulse watches public web pages on a schedule and records a dated signal when a price, stock state, keyword, or selected piece of content changes.

## Primary user
An operator, founder, buyer, ecommerce manager, or competitive-intelligence user who has a small set of pages they cannot afford to keep checking manually.

## Primary action
Create or request a monitor.

## Interface character
Quiet, operational, evidence-first, precise.

## Design constraints
- Use one accent: signal orange `#ff5a36`.
- Use neutral surfaces. No purple/blue gradients, glassmorphism, glow, decorative blobs, or “AI SaaS” backgrounds.
- DM Sans is the UI typeface. IBM Plex Mono is only for labels, timestamps, URLs, and machine-like metadata.
- App navigation is intentionally lower contrast than the working canvas.
- Dense lists are preferred over card walls.
- Cards exist only when they group a real object or decision. Metrics share one container instead of floating independently.
- Radius scale: 4 / 6 / 8px. Large pill shapes are reserved for statuses.
- 1px neutral borders provide structure; shadows are exceptional.
- One primary CTA per region.
- Copy must describe an observable action or outcome. Avoid generic phrases such as “unlock”, “seamless”, “intelligence platform”, or “supercharge”.
- Use real system states: healthy, checking, error, blocked, browser needed, paused.
- Never manufacture testimonials, logos, revenue, users, uptime, or benchmark results.

## Interaction rules
- Entire monitor rows are clickable.
- Controls remain in predictable locations.
- Empty states explain the next action.
- Destructive actions require confirmation.
- A user can test a URL before creating a monitor.
- Monitor detail is evidence-first: current state, reliability, then history.
- Mobile uses a bottom navigation bar rather than compressing a desktop sidebar.

## Research incorporated
The redesign follows patterns repeatedly used in mature SaaS products:
- calmer/dimmer navigation and stronger content hierarchy,
- consistent headers and navigation across views,
- dense list views for scanability,
- full-row click targets,
- explicit current state + historical evidence,
- dashboard metrics that drill into underlying activity,
- real product telemetry instead of decorative “analytics”.
