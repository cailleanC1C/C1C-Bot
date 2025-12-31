# Reaction Roles (Sheet-driven)

Reaction roles are configured in the milestones workbook (`MILESTONES_SHEET_ID`) under the `ReactionRoles` tab. Each row defines which emoji toggles which Discord role and can optionally scope the binding to a specific channel or thread. No environment variables are used for role IDs, channel IDs, or emojis—everything is sheet-driven.

## Sheet columns

Each row in `ReactionRoles` includes the following columns:

- **KEY** – Required. Lowercase tag used by `!reactrole <KEY>` and any code hooks (for example, `leagues`).
- **EMOJI** – Required. Either a Unicode emoji (for example, 🏆 or 👽) or a custom emoji as Discord renders it (for example, `<:amongus_sus:1234567890>` or `<a:animated:987654321>`).
- **ROLE_ID** – Required. Raw Discord role ID.
- **CHANNEL_ID** – Optional. When populated, the reaction role only applies if the wired message is in this channel.
- **THREAD_ID** – Optional. When populated, the reaction role only applies inside the referenced thread (or its parent if reacting inside a thread).
- **ACTIVE** – Optional. Values like `TRUE`, `true`, `1`, or `yes` enable the row; anything else is treated as disabled.
- **NOTES** – Free text; ignored by the bot.

## `!reactrole` command

Only CoreOps/admins can wire reaction roles. To attach reactions to a message:

1) Add a row to the `ReactionRoles` tab with the desired KEY, emoji, role ID, and any channel/thread restrictions.
2) Reply to the target message with `!reactrole <KEY>` (for example, `!reactrole leagues`).
3) The bot loads all ACTIVE rows matching that KEY and attaches their emojis to the message.

Members can react to the attached emoji(s) to gain the configured role and remove their reaction to drop it. Channel and thread restrictions from the sheet always apply.

## Leagues subscription

The weekly leagues announcement now includes a 🏆 footer explaining how to subscribe. The bot automatically attaches the 🏆 reaction via the `ReactionRoles` tab entry keyed to `leagues`. Members who react receive the "C1C League" role; removing the reaction removes the role.

Doc last updated: 2025-12-04 (v0.9.8.2)
