# Sarathi Agent Memory

This file captures portal behavior observed during local runs. It is intentionally
small and practical: use it to avoid rediscovering the same Sarathi quirks.

## Observed Flow

- State selection page uses `#stfNameId` for state dropdown.
- Homepage/mobile update modal must be closed before state selection.
- After selecting Rajasthan, a contactless services modal may appear.
- The modal may contain a direct `Apply for DL Renewal` / `RENEWAL OF DL`
  option; clicking it can jump directly into the DL renewal flow.
- `dlServicesDet.do` is a transitional page. Click `Continue` to reach
  `envaction.do` where DL number, DOB, CAPTCHA, and terms are entered.
- Sarathi CAPTCHA input on DL details page is `#entCaptha` (`captha`, not
  `captcha`).
- DOB programmatic fill can leave a date picker open; blur/Tab after filling.
- `Get DL Details` can accept the form without URL changing. Treat the next
  visible fields/page state as proof, not URL alone.
- Confirming DL details can show `Application already exists...`; this is not a
  validation failure. Accept it and continue.
- In the existing-application branch, Sarathi may not show a separate
  `Renewal of Driving Licence` checkbox/link after confirmation. If the page is
  already showing `Details of the Driving Licence` or applicant/pincode fields,
  the DL renewal service was already selected by the earlier entry point.
- After confirm, the flow can move into authentication/non-eKYC/Aadhaar OTP
  rather than showing a visible renewal-service checkbox.
- Once OTP is generated, the agent must pause and ask the user for the OTP.

## Test Data

- PIN code: `334401`
- Optional email if required: `sipanijai@gmail.com`

## Current Success Definition

The agent is not successful just because it runs. It is successful when it:

- completes the Sarathi flow automatically up to submission/acknowledgement, or
- pauses with a clear user question for required external data such as OTP, and
- resumes after receiving that answer.
