# Mirralith overview housekeeping job

This housekeeping flow keeps the Mirralith and cluster overview Discord channel aligned with the recruitment sheet. It renders configured ranges to PNGs and upserts labeled messages so the channel always shows the latest view.

## Posted content
- Mirralith_read_only tab:
  - ✨ Mirralith • Clan Status (`[MIRRALITH_CLAN_STATUS]`)
  - ✨ Mirralith • Clan Leadership (`[MIRRALITH_LEADERSHIP]`)
- cluster_structure tab:
  - Cluster Structure — Beginner Bracket (`[MIRRALITH_CLUSTER_BEGINNER]`)
  - Cluster Structure — Early Game Bracket (`[MIRRALITH_CLUSTER_EARLY]`)
  - Cluster Structure — Mid Game Bracket (`[MIRRALITH_CLUSTER_MID]`)
  - Cluster Structure — Late Game Bracket (`[MIRRALITH_CLUSTER_LATE]`)
  - Cluster Structure — Early End Game Bracket (`[MIRRALITH_CLUSTER_EARLY_END]`)
  - Cluster Structure — Elite End Game Bracket (`[MIRRALITH_CLUSTER_ELITE_END]`)

Each message includes its label token so the bot can find and update it on subsequent runs.

## Configuration
### Environment variables
- `RECRUITMENT_SHEET_ID` — sheet ID containing Mirralith and cluster_structure tabs.
- `MIRRALITH_CHANNEL_ID` — Discord channel for the Mirralith overview posts.
- `MIRRALITH_POST_CRON` — cron expression (UTC) driving the scheduled refresh.

### KV sheet keys
- Tab names: `MIRRALITH_TAB`, `CLUSTER_STRUCTURE_TAB`.
- Mirralith ranges: `MIRRALITH_CLAN_RANGE`, `MIRRALITH_LEADERSHIP_RANGE`.
- Cluster ranges: `CLUSTER_BEGINNER_RANGE`, `CLUSTER_EARLY_RANGE`, `CLUSTER_MID_RANGE`, `CLUSTER_LATE_RANGE`, `CLUSTER_EARLY_END_RANGE`, `CLUSTER_ELITE_END_RANGE`.

## Usage
- Scheduled: runs according to `MIRRALITH_POST_CRON` and updates the Mirralith channel.
- Manual: administrators can trigger an immediate refresh with `!mirralith refresh` (5-minute cooldown). The command posts status messages in the invoking channel.
- Fault tolerance: missing config or Sheets/Discord errors are logged and skipped per image; the bot and scheduler stay up.

## Troubleshooting
- If logs show `Mirralith spec missing tab or range; skipping`, the bot could not read the Config tab for the listed key; ensure the KV rows exist and the deployment includes the latest config loader.

Doc last updated: 2025-12-04 (v0.9.8.2)

## C1C recruitment ad posting

The `!c1cad` command and `c1c_ad` scheduled job intentionally reuse the Mirralith overview export pattern: Config-tab keys resolve the source tab/range, `get_tab_gid` resolves the worksheet gid, and `export_pdf_as_png` renders the configured range as a PNG.

### Migration rows

Config tab rows:

```text
C1C_AD_TAB = C1C_AD
C1C_AD_IMAGE_RANGE = A1:V42
C1C_AD_TEXT_TAB = C1C_AD_TEXT
C1C_AD_TEXT_ROW = 2
C1C_AD_TARGET_THREAD_ID = 1324313499731755039
C1C_AD_REFRESH_DAYS = 7
```

FeatureToggles row:

```text
c1c_ad,TRUE
```

Create the `C1C_AD_TEXT` tab with row 1 headers:

```text
ad_text | last_posted_at_utc | last_image_message_id | last_text_message_id | last_post_status | last_post_error | updated_at_utc
```

### Operation

- Manual refresh: `!c1cad` (admin-gated) checks `c1c_ad`, renders `C1C_AD!A1:V42`, reads `ad_text` from configured row 2, deletes prior stored bot message IDs, posts image then text, and writes the new message IDs/status back to the header-driven row.
- Scheduled refresh: every configured `C1C_AD_REFRESH_DAYS`; it checks `last_posted_at_utc` before posting so restarts do not duplicate the ad.
- Safe skips/failures: missing toggle/config/header, empty text, render failure, or missing target thread logs a short reason and does not post partial image-only or text-only ads.
