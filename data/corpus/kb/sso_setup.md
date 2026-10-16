# Single Sign-On (SSO) Setup

SSO is available on the Business and Enterprise plans. Acme Cloud supports SAML 2.0 and OpenID Connect (OIDC). Supported identity providers include Okta, Microsoft Entra ID (Azure AD), Google Workspace, OneLogin and any standards-compliant SAML 2.0 IdP.

## Configure SAML 2.0
1. In Acme Cloud, go to Admin Console > Security > Single sign-on and choose SAML 2.0.
2. Copy the ACS URL (https://sso.acmecloud.io/saml/acs) and the Entity ID (urn:acmecloud:sp) into your IdP.
3. Upload your IdP metadata XML or paste the IdP SSO URL and the X.509 signing certificate.
4. Map the NameID to the user's email address.
5. Click Test connection before enforcing SSO.

## Enforcing SSO
After a successful test, admins can enable "Require SSO for all members". At least one owner account keeps password login as a break-glass account. Just-in-time (JIT) provisioning creates users on first login; SCIM 2.0 provisioning is available on Enterprise only.

## Troubleshooting SSO errors
- SSO-401: The SAML response signature is invalid. The X.509 certificate uploaded to Acme Cloud does not match the IdP signing certificate. Re-upload the current certificate; certificates rotated in the IdP must be re-uploaded.
- SSO-403: The user is not assigned to the Acme Cloud application in the IdP. Assign the user or group in your IdP.
- SSO-409: Email mismatch. The NameID email does not match an existing Acme Cloud account and JIT provisioning is disabled.
- SSO-422: Clock skew. The SAML assertion is outside its validity window; ensure IdP server time is synced via NTP (tolerance is 3 minutes).
