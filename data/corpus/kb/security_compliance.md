# Security and Compliance Overview

## Encryption
All files are encrypted at rest with AES-256 and in transit with TLS 1.2 or higher. Encryption keys are managed in a hardware security module (HSM)-backed key management service and rotated every 90 days. Enterprise customers can bring their own keys (BYOK) using AWS KMS or Azure Key Vault.

## Certifications
Acme Cloud holds SOC 2 Type II and ISO/IEC 27001:2022 certifications. SOC 2 reports are available under NDA from the Trust Center. Acme Cloud is not HIPAA certified, but signs Business Associate Agreements (BAAs) for Enterprise customers.

## Data residency
Enterprise customers can choose where file content is stored: United States (Virginia and Oregon), European Union (Frankfurt and Dublin) or India (Mumbai). Metadata required for global routing may be processed in the United States.

## Access controls
Business and Enterprise support admin roles, device approval, and IP allow-listing. Two-factor authentication can be enforced for all members on every paid plan.

## Vulnerability disclosure
Security researchers can report issues to security@acmecloud.io. Acme Cloud runs a private bug bounty program and performs third-party penetration tests twice per year.

## Incident notification
Acme Cloud notifies affected customers of a confirmed personal-data breach within 72 hours of becoming aware of it.
