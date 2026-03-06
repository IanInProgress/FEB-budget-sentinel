# FEB Purchase Bot

A Slack-integrated budget management system for Formula Electric Berkeley. Members submit purchase requests, managers review and approve via reactions, and the bot automatically updates Google Sheets in real-time.

## Features

- **Real-time Budget Verification** — Instantly check if purchases fit within team budget
- **Google Sheets Integration** — Two-way sync with budget spreadsheets (read + write)
- **Smart Item Matching** — Fuzzy search to find similar budget line items
- **Confirmation Dialog** — Interactive Confirm/Cancel buttons before submission
- **Screenshot Attachments** — Upload receipts/screenshots in threaded replies
- **Manager Notifications** — Compact Block Kit reports posted to manager channel
- **Reaction-Based Approval** — Managers react with ✅/❌ to approve/reject
- **Automatic Sheet Updates** — Approved purchases update "Actual Spending" column
- **Member Notifications** — DM notifications when requests are approved/rejected

## Requirements

- Python 3.9+
- Slack workspace with bot permissions:
  - **OAuth Scopes**: `commands`, `chat:write`, `channels:history`, `groups:history`, `reactions:read`, `channels:join`
  - **Event Subscriptions**: `message.channels`, `message.groups`, `reaction_added`
  - **Slash Commands**: `/purchase`
- Google Sheets API access with service account
- Google Sheet with budget data (Columns: Item Name, Estimated Budget, Actual Spending)

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
   SLACK_MANAGER_CHANNEL_ID=C123ABC456
   GOOGLE_SHEET_ID=your-spreadsheet-id
   GOOGLE_SERVICE_ACCOUNT_FILE=google-service-account.json
   LOG_LEVEL=INFO
   PORT=3000
   ```

5. **Configure Google Sheets API**
   - Create a service account in Google Cloud Console
   - Download the JSON credentials file
   - Save it as `google-service-account.json` in the project root
   - Share your Google Sheet with the service account email (as Editor)
   - **Important**: Remove protection from Column C (Actual Spending) or grant the service account edit access to that column

## Configuration

### Subteam Aliases

The bot supports the following subteams and their aliases:

- **Admin** — `admin`
- **Dynamics** — `dynamics`, `dyn`
- **Chassis** — `chassis`
- **Powertrain** — `powertrain`, `pt`
- **Composites** — `composites`, `comp`
- **Brakes/Ergo** — `brakes`, `ergo`
- **Accumulator MechE** — `meche`, `mech`, `accumulator`
- **EECS** — `eecs`
- **Aero** — `aero`
- **Auto** — `auto`
- **Manufacturing** — `manufacturing`, `mfg`

You can customize aliases by setting the `SUBTEAM_ALIASES_JSON` environment variable.

### Google Sheets Format

Each subteam should have its own worksheet/tab in the spreadsheet with:
- **Column A**: Item Name
- **Column B**: Estimated Budget
- **Column C**: Actual Spending (auto-updated by bot)
- Header row at row 1

## Usage

### Member Workflow

1. **Submit Purchase Request**
   ```
   /purchase <subteam>, <item name>, <amount>
   ```
   
   **Examples:**
   ```
   /purchase eecs, zipties, 25
   /purchase pt, Engine mounts, 150
   /purchase dyn, Dampers, 2500.00
   ```
   
   Note: Item names can be with or without quotes

2. **Confirm Request**
   - Review the budget report shown to you
   - Click **Confirm** to proceed or **Cancel** to abort

3. **Attach Screenshot** (Optional)
   - After confirming, upload a receipt/screenshot in the thread
   - The bot will automatically detect the upload

4. **Wait for Approval**
   - Your request is posted to the manager channel
   - You'll receive a DM when approved or rejected

### Manager Workflow

1. **Review Requests**
   - Purchase requests appear in the manager channel
   - Compact report shows: subteam, member, item, amount, budget status, screenshot

2. **Approve or Reject**
   - React with ✅ (`:white_check_mark:`) or 👍 (`:+1:`) to **approve**
   - React with ❌ (`:x:`) or 👎 (`:-1:`) to **reject**

3. **Automatic Updates**
   - Approved purchases update the Google Sheet (Column C: Actual Spending)
   - Member receives a DM notification with the decision
   - Budget cache refreshes automatically

### Budget Reports

The bot provides:
- **Within Budget** — Purchase fits within remaining budget
- **Over Budget** — Purchase exceeds available funds
- **Similar Items** — If exact match not found, suggests closest matches
- **Budget Info** — Shows remaining balance, actual spending, and estimated budget

## Running the Bot

```bash
python app.py
```

The bot will start on port 3000 (or the port specified in your `.env` file).

### Slack App Configuration

Configure your Slack App at [api.slack.com/apps](https://api.slack.com/apps):

1. **OAuth & Permissions** → Add these scopes:
   - `commands`
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `reactions:read`
   - `channels:join`

2. **Slash Commands** → Create `/purchase` command:
   - Request URL: `https://your-domain.com/slack/commands`

3. **Event Subscriptions** → Enable and subscribe to:
   - `message.channels`
   - `message.groups`
   - `reaction_added`
   - Request URL: `https://your-domain.com/slack/events`

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
Member Types /purchase
       ↓
Budget Check (Google Sheets)
       ↓
Confirmation Dialog (Confirm/Cancel)
       ↓
Screenshot Upload (Thread)
       ↓
Manager Channel Notification
       ↓
Manager Reacts ✅ or ❌
       ↓
Google Sheet Updated (if approved)
       ↓
Member Gets DM Notification
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

Column C (Actual Spending) is protected in your Google Sheet. Fix:
- **Option 1**: Go to Data → Protected sheets and ranges → Delete Column C protection
- **Option 2**: Add your service account email to the protection's allowed editors

### Bot Not Responding to Reactions

1. Check Event Subscriptions are enabled at [api.slack.com/apps](https://api.slack.com/apps)
2. Verify `reaction_added` event is subscribed
3. Confirm `reactions:read` scope is granted
4. Restart the bot after code changes: `python app.py`

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
