# Security

Report vulnerabilities through this repository's GitHub **Security → Report a vulnerability**
feature. Do not put credentials, exploit details or private user data in public issues.

Security fixes target the latest release. Use authenticated router endpoints, restrict
network listeners to intended interfaces, and grant Computer/Workspace capabilities only
to trusted agents. Unauthenticated LAN mode is for explicitly isolated deployments.

Browser, terminal and file tools can act on the caller's computer. Review capability grants
and retain server-side authorization. Never embed Cloud tokens in agent source or images.
