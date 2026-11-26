# The Division 2 Status Watch

A small web app and background monitor for The Division 2. It checks Ubisoft's status display every 3, 5, 10, 15, 20, 25, 30, or 60 minutes and can send a notification when the reported status changes to any status you select.

The monitor reads the status widget linked from [Ubisoft's The Division 2 status page](https://www.ubisoft.com/en-us/game/the-division/the-division-2/status). If the page is reachable but does not expose a clear live status, the dashboard shows Online as its default and the monitor waits for the next check. If the page cannot be fetched, the dashboard shows Unknown. It does not treat the four-item status legend as the current game status.

## Deploy in Portainer

1. Put this folder on the server, or add it to a Git repository that Portainer can deploy.
2. In Portainer, choose **Stacks** → **Add stack**. Select this folder or repository and use compose.yaml.
3. Add a strong WEB_PASSWORD environment variable in the stack settings. WEB_USERNAME defaults to admin; you can change it. The stack will not start without WEB_PASSWORD.
4. Deploy the stack. Open http://SERVER-IP:3274 (or the host port set with PORT) and sign in with those credentials.
5. Choose the interval, select which status changes should trigger alerts, choose a notification method, enter the delivery settings, and save. Online (Operational) is selected by default to preserve the original recovery alert behaviour.

The named division2-status-data volume stores settings and last status across restarts. The web page uses HTTP Basic Authentication. Put it behind HTTPS before exposing it to the public internet.

## Gotify push notifications

Choose **Gotify push**, enter your Gotify server URL, and paste an application token from an application created in Gotify. Select **Send test push** to check the setup before saving; the test uses the values currently entered and does not change your saved settings. If the token field is blank, the saved token is used. The app posts selected status notifications to Gotify's message endpoint with the token in the X-Gotify-Key header. You can use a server URL with a reverse-proxy base path, or paste a URL ending in /message. Gotify priority ranges from 0 to 10; the default of 5 produces a sound in the Gotify Android app. Install and connect the Gotify app on your phone to receive push notifications.

See Gotify's [push message instructions](https://gotify.net/docs/pushmsg) and [priority guide](https://gotify.net/docs/priority) for creating application tokens and choosing notification behaviour.

## Email notifications

Use your mail provider's SMTP server, port, and connection security setting. Common combinations are STARTTLS on port 587 or SSL/TLS on port 465. Enter the sender address accepted by that provider. An SMTP app password may be required instead of the account password.

## WhatsApp notifications

WhatsApp alerts are sent through Twilio. Enter your Twilio Account SID and Auth Token, your WhatsApp number, and a WhatsApp-enabled sender assigned to your Twilio account. Use international number format, such as +15551234567; the app adds the WhatsApp address prefix.

For proactive alerts outside WhatsApp's 24-hour customer service window, create an approved template in Twilio's Content Template Builder and enter its Content SID. The template should include `{{1}}` for the reported status and `{{2}}` for the Ubisoft status page URL. Without a Content SID, the app sends a free-form message, which can only be delivered while the 24-hour window is open. Twilio may require the recipient to join your WhatsApp Sandbox or opt in to your approved sender. See Twilio's [WhatsApp quickstart](https://www.twilio.com/docs/whatsapp/quickstart) and [template sending guide](https://www.twilio.com/docs/content/send-templates-created-with-the-content-template-builder).

## Behavior

- The first successful check records a baseline and does not send an alert.
- Select any combination of Online (Operational), Maintenance, Problems, and Outage. A notification is sent once when Ubisoft changes to a selected status; staying in that status does not repeat the alert.
- Online (Operational) is selected by default, matching the previous recovery notification behaviour.
- WhatsApp delivery uses Twilio. A Twilio-approved template is needed for proactive messages outside a 24-hour customer service window.
- Temporary fetch failures show as Unknown; when no unambiguous live status is found, the dashboard displays Online as its default. Neither case counts as a status change.
- Notification credentials are stored in the persistent Docker volume and are not returned to the browser after saving.
- The monitor reports what Ubisoft's page publishes; it cannot verify connectivity from a particular player's device or platform.
