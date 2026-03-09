# FEB Purchase Bot

A Slack-integrated budget management system for Formula Electric Berkeley. Members submit purchase requests, managers review and approve via reactions, and the bot automatically updates Google Sheets in real-time.

## Features

### Budget Management
- **Dual-Level Budget Validation** — Validates purchases against both individual item budgets AND team-wide available budgets
- **Reference ID Tracking** — Every budget item has a unique ID (e.g., ADMIN-001, EECS-042) for precise tracking
- **Pending & Actual Spend** — Tracks approved spending (pending) separate from reimbursed spending (actual). Approved purchases stay in pending until `/reimburse` moves them to actual
- **Bank Balance Management** — Maintains club-wide available fund balance, auto-deducted on each approval
- **Comprehensive Audit Trail** — 18-column Purchases_Log records every transaction with budget snapshots before/after
- **Unaccounted Item Support** — Handle off-budget purchases using -000 reference IDs (e.g., ADMIN-000 Office Supplies)

### Google Sheets Integration
- **Two-Way Sync** — Reads budget data and writes spending updates in real-time
- **Auto-Creation** — Automatically creates _Config and Purchases_Log tabs if missing
- **Auto-Expanding Columns** — Adjusts sheet structure for existing spreadsheets
- **Tab-Level Budget Fallback** — Single available budget value can apply to entire subteam

### Purchase Request Workflow
- **Message-Based Submission** — Type `command_purchase: <ref_id>, <amount>, <reason>` with receipt image attached
- **Interactive Confirmation** — Review budget report and confirm/cancel before posting to managers
- **Built-in Tutorial** — `/tutorial` command shows usage guide with examples and dismissible button

### Manager Approval Workflow
- **Smart Recommendations** — Reports show ✅ RECOMMEND_APPROVE, ❌ RECOMMEND_REJECT, or ⚠️ RECOMMEND_CONSIDER based on budget analysis
- **Budget Before/After Display** — Shows exact impact on item budget, subteam budget, and bank balance
- **Thread-Based Approval** — Managers reply with ✅ or ❌ emoji in thread (receipt image also posted in thread)
- **Rejection Reasons** — Manager types reason in thread, bot forwards to member via DM
- **Automatic Updates** — Approved purchases instantly update pending spend and bank balance (use `/reimburse` later to move pending→actual)

### Notifications & Communication
- **Manager Channel Reports** — Formatted notifications with all purchase details, budget status, and recommendations
- **Member DMs** — Instant notifications when requests are approved or rejected (with reason if rejected)
- **Auto-Join Channels** — Bot automatically joins public channels when tagged to avoid permission errors

## Requirements

- Python 3.9+
- Slack workspace with bot permissions:
   - **OAuth Scopes**: `chat:write`, `chat:write.public`, `commands`, `channels:history`, `groups:history`, `channels:join`
  - **Event Subscriptions**: `message.channels`, `message.groups`
- Google Sheets API access with service account
- Google Sheet with budget data (see Google Sheets Format section below)

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/IanInProgress/FEB-budget-sentinel.git
   cd FEB-budget-sentinel
   ```

2. **Create and activate virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   
   Create a `.env` file in the project root:
   ```env
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   SLACK_SIGNING_SECRET=your-signing-secret
   MANAGER_CHANNEL_ID=C123ABC456
   GOOGLE_SHEET_ID=your-spreadsheet-id
   GOOGLE_SERVICE_ACCOUNT_FILE=google-service-account.json
   PURCHASE_COMMAND_KEYWORD=command_purchase:
   LOG_LEVEL=INFO
   PORT=3000
   ```

5. **Configure Google Sheets API**
   - Create a service account in Google Cloud Console
   - Download the JSON credentials file
   - Save it as `google-service-account.json` in the project root
   - Share your Google Sheet with the service account email (as Editor)
   - **Important**: Grant the service account edit access to all columns that the bot needs to update

## Configuration

### Google Sheets Format

Each subteam should have its own worksheet/tab in the spreadsheet with the following columns:

- **Column A**: Reference ID (e.g., ADMIN-001, EECS-042)
- **Column B**: Item Name
- **Column C**: Estimated Budget
- **Column D**: Pending Spend (auto-updated by bot)
- **Column E**: Actual Spend (auto-updated by bot on approval)
- **Column F**: Available Budget (subteam-level budget remaining)
- **Column G**: Total Budget
- **Header row at row 1**

**Setting Up Available Budget (Column F)**:
- Fill ONE cell in Column F (typically row 2) with the formula: `=G2-(SUM(D:D)+SUM(E:E))`
- This calculates: Total Budget minus all Pending and Actual spending across the entire subteam
- The bot will automatically apply this single value to all items in the tab for subteam-level budget validation
- **Note**: The bot calculates item-level remaining budgets internally (Estimated - Pending - Actual), you only need to set up the subteam-wide available budget in Column F

#### _Config Tab (Auto-Created)

The bot automatically creates a `_Config` tab with:
- **request_counter**: Auto-incrementing counter for unique request IDs
- **bank_available**: Club-wide funds balance (updated on approvals)

#### Purchases_Log Tab (Auto-Created)

The bot automatically creates a `Purchases_Log` audit trail with 18 columns tracking all transactions, including budget snapshots before/after each change.

### Reference ID Prefixes

The bot uses the following reference ID prefixes to identify subteams:

- **ADMIN** → Admin
- **DYNA** → Dynamics
- **CHAS** → Chassis
- **POWER** → Powertrain
- **COMP** → Composites
- **ERGO** → Brakes/Ergo
- **MECH** → Accumulator MechE
- **EECS** → EECS
- **AERO** → Aero
- **AUTO** → Auto
- **MANU** → Manufacturing

**Unaccounted Items**: Use `-000` suffix (e.g., `ADMIN-000`) for items not in the planned budget.

### Command Keyword

- `PURCHASE_COMMAND_KEYWORD` (default: `command_purchase:`): The keyword that triggers purchase request parsing in messages

## Usage

### Member Workflow

0. **View Tutorial (Optional)**
   - Run `/tutorial` in any channel
   - The bot posts a guide message with a **Delete tutorial** button

0. **View Reimburse Command (Treasurers Only)**
   - Run `/reimburse` in any channel to process reimbursements
   - This moves approved spending from Pending (Column D) to Actual (Column E)
   - Only use after payment/reimbursement is completed

1. **Submit Purchase Request with Image Attachment**
   
   Send a message in purchase-request channel with the format:
   ```
   command_purchase: <reference_id>, <amount>, <reason>
   ```
   
   **Attach your receipt image to the same message**
   
   **Examples for tracked items:**
   ```
   command_purchase: EECS-025, 42.50, Zipties for cable management
   command_purchase: POWER-103, 150.00, Engine mount replacement
   command_purchase: DYNA-008, 2500.00, New dampers for suspension
   ```
   
   **Examples for unaccounted items (not in budget plan):**
   ```
   command_purchase: ADMIN-000 Office Supplies, 50.00, Need for team workspace
   command_purchase: EECS-000 USB Cables, 25.00, Additional development tools
   ```
   
   *Note: The command keyword is configurable via the `PURCHASE_COMMAND_KEYWORD` environment variable (default: `command_purchase:`)*
   
   *Note: For unaccounted items (ending in -000), you must provide an item name after the reference ID*

2. **Confirm Request**
   - The bot replies with a confirmation dialog showing your request details
   - Click **Confirm** to submit or **Cancel** to abort

3. **Wait for Approval**
   - Your request is posted to the manager channel with a budget recommendation
   - You'll receive a DM when approved or rejected

### Manager Workflow

1. **Review Requests**
   - Purchase requests appear in the manager channel
   - Compact report shows: subteam, member, reference ID, item, amount, budget status, and recommendation
   - Recommendations include: ✅ RECOMMEND_APPROVE, ❌ RECOMMEND_REJECT, ⚠️ RECOMMEND_CONSIDER_APPROVAL
   - Budget before/after amounts displayed for transparency
   - Screenshot is posted as a threaded reply under that report message

2. **Approve or Reject**
    - Reply in the thread with only one emoji:
       - To **approve**: send `✅` (or `:white_check_mark:`)
       - To **reject**: send `❌` (or `:x:`)
   - For rejections, the bot will prompt you to provide a reason in the same thread
   - The bot forwards the rejection reason to the requester via DM

3. **Visual Feedback**
   - When approved: ✅ reaction appears on the original purchase request message
   - When rejected (after reason provided): ❌ reaction appears on the original purchase request message

4. **Automatic Updates**
   - Approved purchases update the Google Sheet:
     - Column D: Pending Spend → remains (includes all approved purchases)
     - _Config: bank_available → deducted by purchase amount
   - Purchases_Log audit trail records complete transaction details
   - Member receives a DM notification with the decision
   - Budget cache refreshes automatically
   - **Note**: Pending spend moves to Actual spend when treasurer runs `/reimburse` command after payment is complete

### Budget Reports

The bot evaluates purchases against two budget levels:
- **Item Budget** — Individual line item budget (Estimated - Pending - Actual)
- **Subteam Available Budget** — Team-wide spending limit (Column F = Total - Sum of all Pending - Sum of all Actual)

Status indicators:
- **✅ Within Budget** — Purchase fits within both item and subteam budgets
- **⚠️ Over Item Budget** — Exceeds item budget but within subteam budget
- **❌ Over Subteam Budget** — Exceeds team-wide available budget
- **🆕 Unaccounted Item** — Not in planned budget (uses -000 reference ID)

All budget reports show remaining balances and before/after amounts for transparency.

## Running the Bot

```bash
python app.py
```

The bot will start on port 3000 (or the port specified in your `.env` file).

### Slack App Configuration

Configure your Slack App at [api.slack.com/apps](https://api.slack.com/apps):

1. **OAuth & Permissions** → Add these scopes:
   - `chat:write`
   - `chat:write.public`
   - `commands`
   - `channels:history`
   - `groups:history`
   - `channels:join`

2. **Event Subscriptions** → Enable and subscribe to:
   - `message.channels`
   - `message.groups`
   - Request URL: `https://your-domain.com/slack/events`

3. **Slash Commands** → Create commands:
   - Command: `/tutorial`
   - Command: `/reimburse`
   - Request URL: `https://your-domain.com/slack/commands` (same for both)

4. **Interactivity & Shortcuts** → Enable:
   - Request URL: `https://your-domain.com/slack/commands`

5. **Install App** → Install to your workspace and copy the Bot Token

## Development

### Running Tests

```bash
pytest
```

### Running Locally

```bash
# Activate virtual environment
source .venv/bin/activate

# Start the bot
python app.py
```

Use a tool like [ngrok](https://ngrok.com/) to expose your local server for Slack webhooks:
```bash
ngrok http 3000
```

Then update your Slack App's Request URLs to use the ngrok URL.

## Workflow Diagram

```
Member Sends Message with Reference ID + Receipt
       ↓
Bot Parses Command & Validates Reference ID
       ↓
Budget Check (Google Sheets)
       ↓
Confirmation Dialog (Confirm/Cancel)
       ↓
Manager Channel Notification + Screenshot in Thread
       ↓
Manager Sends ✅ or ❌ Emoji in Thread
       ↓
Google Sheet Updated (if approved)
  - Pending Spend stays (committed)
  - Bank balance deducted
  - Purchases_Log updated
       ↓
Member Gets DM Notification
       ↓
(Later) Treasurer runs /reimburse
  - Pending → Actual
  - After reimbursement complete
```

## License

MIT

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

    ├── conftest.py
    ├── test_parser.py
    └── test_budget_checker.py
```

## Troubleshooting

### "You are trying to edit a protected cell or object"

Columns in your Google Sheet are protected. Fix:
- **Option 1**: Go to Data → Protected sheets and ranges → Remove protection from columns D, E (and any other bot-updated columns)
- **Option 2**: Add your service account email to the protection's allowed editors

### Bot Not Responding to Messages

1. Check Event Subscriptions are enabled at [api.slack.com/apps](https://api.slack.com/apps)
2. Verify `message.channels` and `message.groups` events are subscribed
3. Confirm all required OAuth scopes are granted
4. Check that your message contains the command keyword (default: `command_purchase:`)
5. Restart the bot after code changes: `python app.py`

### Invalid Reference ID Error

- Ensure reference IDs match the format: `PREFIX-###` (e.g., `ADMIN-001`, `EECS-042`)
- Valid prefixes: ADMIN, DYNA, CHAS, POWER, COMP, ERGO, MECH, EECS, AERO, AUTO, MANU
- For unaccounted items, use `-000` suffix and include item name: `ADMIN-000 Item Name`

### "not_in_channel" Error

The bot will automatically join public channels. For private channels, manually invite the bot: `/invite @BotName`

### Invalid Syntax Errors

The command format is:
```
/purchase <subteam>, <item name>, <amount>
```

Common mistakes:
- Missing commas between fields
- Invalid subteam name (see Configuration section for valid aliases)
- Non-numeric amount (use numbers only, $ is optional)

## Development

MIT

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
