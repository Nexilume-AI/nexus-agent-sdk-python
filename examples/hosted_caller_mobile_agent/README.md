# Hosted caller Mobile validator

This example is a Docker-hosted FastMCP Agent. Nexus Cloud supplies a short-lived,
Run-scoped Mobile delegate after the caller grants the declared scopes and attaches
their own Android device. The container never receives the device pairing token or
the caller's Nexus credentials.

The `validate_caller_mobile` tool publishes Plan and Browser state, pauses for the
mandatory high-risk `type_text` approval, and reports a friendly Chat result. Set the
managed Agent and current Agent Version to `mobile_requirement=required` with
`mobile_capabilities` equal to the tool's `mobile_scopes` before deployment.
