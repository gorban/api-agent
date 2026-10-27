# Bedrock prerequisites


- Python 3.13 or newer with the optional dependencies above, and currently Linux
  or macOS/POSIX process groups. Native Windows isolation is not qualified.
  Prebuilt PyPI wheels normally avoid native toolchains; source builds need the
  toolchain required by their packages.
- An AWS account, chosen Region, working HTTPS connectivity and permission for
  Mantle model listing/inference. The HTTP client verifies TLS, disables redirects,
  does not inherit ambient proxy configuration and performs no automatic retries.
  AWS account creation, permissions and billing are not provisioned by the app.
- AWS CLI is optional when credentials are already available through supported
  files/workload sources, or when using explicit access_key/api_key. A configured
  credential_process profile requires its actual helper executable on the app's
  PATH; custom adapters own their additional prerequisites.
- For CLI-assisted IAM Identity Center login, use AWS CLI v2 with
  aws configure sso and aws sso login --profile NAME. Browser PKCE or the documented
  device-code flow is an external setup step, not automatic provider login.
- For console-based aws login, use AWS CLI 2.32.0 or newer and appropriate sign-in
  permissions. Login-session profiles need the PyPI CRT extra, now included in
  both api-agent[bedrock] and the selected provider dependency declaration. For an
  older environment that has only plain boto3, install the compatible extra:

      python -m pip install 'boto3[crt]>=1.41,<2'

  Install CLI v2 through the [official platform instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
  only for a workflow that needs it. Verify aws --version in the environment
  running the application; a host installation may not be visible in a container.

- In the masked sign-in dialog, Ctrl+V reads the current OS clipboard using
  optional `wl-paste` from `wl-clipboard` on Linux Wayland or `pbpaste` on macOS.
  Install the helper in the launcher environment and allow desktop-session access.
  Without a usable helper, use the terminal's Paste action (commonly Ctrl+Shift+V
  on Linux). Private terminal paste preserves the full text; native authorization
  code handling removes ASCII formatting whitespace. Other credentials retain
  their exact input. No Python clipboard package is needed.

See [Boto3 credentials](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html),
[AWS console login](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
and [IAM Identity Center](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).
No CLI, system package, browser or trust-store change is performed automatically.
