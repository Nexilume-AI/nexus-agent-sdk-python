# Pi Coding Agent

Chat with the native [Pi Coding Agent](https://github.com/earendil-works/pi) in Nexus Private Display. Ask Pi to explain code, create or edit files, run small Python or Node.js programs, and deliver downloadable results.

## Start a conversation

1. Open **Private Display** and send a message.
2. Choose and explicitly approve your own Nexus Router or an OpenAI-compatible endpoint, model and API key. New conversations reuse your valid Pi model authorization within the same caller and workspace scope. Approval is requested when no valid grant remains; expired or revoked grants are never reused. Explicit caller-supplied MCP preauthorization is also supported. Model credentials are handled by Nexus authorization, never entered in chat or shared with the code sandbox.
3. After approval, send your message again. Continue in the same Run to retain the conversation, workspace and valid authorization without repeated approval. Download published files from the **Files** panel.

The native Pi SDK performs the model/tool loop. Pi can read, write and edit files, run shell commands in its sandbox, and publish files. Text replies and tool activity appear during execution. Cancel a turn from Private Display.

## MCP tools

- **pi_chat** — start a chat task using `message`; continue through the same Private Display Run. An optional `authorization_id` selects an existing caller-owned grant.
- **authorize_pi_model** — request or inspect model approval. Supports Nexus, OpenAI and custom OpenAI-compatible endpoints.
- **pi_status** — inspect the installed version and service limits without calling a model.

Use the Marketplace connection settings for the authenticated public MCP endpoint. Private Display provides the intended continuous chat experience; independent MCP Runs have separate workspaces.

## Workspace and limits

Each caller/Run receives an offline Linux container with Python 3, Node.js, Bash, Git and ripgrep. It has no host filesystem mounts, network access, Docker socket or provider credentials. Computer attachment is optional. Attach your own Computer in Private Display to enable `computer_list`, `computer_read`, `computer_write` and `computer_exec` within the Run-authorized working directory and capabilities. These tools operate your device through Nexus; command output appears in Shell. In **Run context → Workspace**, browse an accessible folder on the device and select **Use folder**. Stop the current task before changing its folder. The choice applies to this Run; Pi cannot change this selection by itself. Shell commands run with the device user’s operating-system permissions.

With **browser.control** approved, Pi can search the web, open pages, read their content, click observed elements, fill fields and scroll. The Browser panel displays screenshots from an isolated browser on your attached Computer. It does not reuse your personal browser's cookies or signed-in sessions. Website access depends on that Computer's network and the website's restrictions.

The offline `workspace_*` tools remain separate. Pi cannot choose or attach another device, and no device credentials are exposed to the model. Detachment, revoked permissions or an offline device prevents the corresponding operation. Desktop mouse/keyboard control is not included. No Mobile connection is required.

- Workspace: 128 MiB; expires after one idle hour. Service restart or a failed/cancelled turn resets the live workspace. Already published files remain attached to the Run.
- Two sessions per caller, eight sessions total; two concurrent turns with a bounded FIFO queue. A caller may have one queued or active turn. A shared API key represents the same caller.
- Per turn: up to 10 minutes, 12 model requests and 30 workspace tool calls; up to 40 turns per live workspace.
- Each shell command is limited to 60 seconds. Up to eight files, each at most 8 MiB, can be published per turn.
- Chat input is text. The offline sandbox has no Internet access or GPU. Attached Computer operations use that device's permissions and network; browser access requires browser.control approval.

## Pricing and verification

The Agent listing is **free**. Usage of your selected model is subject to your provider's billing and limits.

Deployment acceptance used a real caller-approved `gpt-6-sol` model to create and execute a Python program, publish its file, and continue the conversation while reading the existing file. Protected downloads were checked for content hashes, byte ranges and rejected anonymous access. This is a hosted integration of Pi version 1.4.0, not an official Pi-hosted service.
