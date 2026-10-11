# Accepted design: concept B

Reference: `concept-b.png`, 1536 × 1024. Root selected B after inspecting both generated concepts.

- Palette: true white app canvas, warm neutral outer shell `#f6f5f2`, forest green `#225d4a`, pale sage transfer card `#edf4ef`, slate body text and fine neutral borders. No gradients or raster media.
- Layout: slim left rail; restrained top header; flexible conversation column and separated 430 px account sidebar at reference width; composer fixed within chat; scroll the conversation independently.
- Typography: local Noto Sans SC with Microsoft YaHei and sans-serif fallbacks; no external font request. At the reference viewport, heading 38 px, message and controls 18 px, secondary labels 12–15 px, transfer amount 48 px. Smaller desktop/mobile breakpoints scale separately. Amounts use tabular numbers.
- Icons: Lucide outline 1.8–2 px, 16–24 px, dark green or slate. Brand is code-native Chinese 银 text, not a raster logo.
- Components: neutral outlined quick actions; pale chat bubbles; sage confirmation with strong amount and thin dividers; compact account rows and ruled transaction list; solid green primary buttons.
- Mobile: bottom navigation, hidden account sidebar available through account/transaction tabs, single column chat and fluid transfer card. Keyboard focus visible; reduced motion honored.

## Approved functional deviations

- Only working navigation: 对话 / 账户 / 交易. Generated 转账 / 业务 / 帮助 / 我的 entries omitted, because transfer and questions are handled in chat.
- Neutral greeting 今天，想办理什么？ instead of fixed 上午好.
- Real server-provided simulated user, account names, amounts, transaction descriptions/dates replace illustrative image data.
- Starts with an honest welcome rather than an invented prior conversation; actual user input and server replies populate messages and plans.
- Expandable transfer details show account identifiers, recipient identifier, expiry and execution timing; references expand into source excerpts.
- Refresh affordance replaces inert chevrons on account headings; no attachment icon because attachments are outside this slice.
- Chat uses SSE status/token events for progress and an authoritative final result. Non-streaming fallback is permitted only for initial HTTP 404/405 route rejection; accepted streams never automatically replay. Errors retain the input.
- Server replies render safe Markdown, including semantic tables and lists, because live DS responses use these forms. Raw HTML is disabled.

## Generated concept prompts

Both variants generated with the built-in ImageGen tool, one call each. A: three-column light Chinese banking workspace, wide navigation, white confirmation block, evergreen accent. B: slim rail, white canvas on warm neutral shell, pale sage transfer approval block, evergreen accent, compact right account overview and recent transactions. Full prompts are recorded in the task's tool history. Illustrative balances and sample transfer were explicitly requested only to demonstrate hierarchy, not used in code.
