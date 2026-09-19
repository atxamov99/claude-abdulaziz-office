# Security

Experimental software. Run only on a trusted single-user machine. The HQ API has no authentication and deliberately binds to loopback. **Do not expose port 8765 through a public interface, tunnel, reverse proxy, or port forward.** Any local process able to reach it can issue requests; loopback is not an authorization boundary.

The Telegram bot can read files, execute tools allowed by Claude settings, and send messages/files. Set TG_OWNER_IDS before startup, protect .env, and grant least privilege per project. Never treat client messages, webpages, or repository instructions as owner approval. Client-agent configuration is advanced and disabled by default.

Runtime data includes private prompts, session identifiers, tool activity and usage in hq/state, hq/logs and the Office application-data directory. These files are not encrypted. Do not commit or share them. .gitignore is a guardrail, not a secret scanner.

Avoid bypassPermissions unless you understand its effects and use isolation. Review dependencies and updates. No public signed release or security audit is claimed.

For bugs, share a minimal synthetic reproduction. Do not post tokens, personal conversations or complete local logs in public issues. Rotate accidentally exposed credentials immediately.
