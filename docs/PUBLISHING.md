# GradeGuard publishing

The active local source and app are in `C:\SchoelTools\SchoelGradeGuard`.
Complete local release packages are under `releases\<version>`. Generated
packages, installers and signing material are excluded from source control.

Source repository: https://github.com/jconniv/Schoel-gradeguard

Publish the working branch and open a pull request against `main`. The Windows
checks run the regression suite and calculation audit. A tagged pilot release
can distribute the reviewed build while the pull request awaits independent
review. Do not treat that as approval to merge or as field validation.

The SharePoint release destination is the devteam site's Documents library:
`General/Applications/JoeJoe/Desktop Apps/Schoel GradeGuard`.
Create a versioned subfolder, upload the release package and verify the remote
file metadata/checksums. Keep earlier releases. The older `Schoel-gradeguard-main`
folder is historical; its name is not proof that it contains current source.

Use the existing WiX `installer/Product.wxs` and Schoel signing certificate.
If the usable signing key is installed in LocalMachine, signtool requires `/sm`;
the default search uses CurrentUser. Verify the actual distributed EXE and MSI
with signtool and Get-AuthenticodeSignature after signing. Certificates and
private keys must not be included in repository source or release packages.

`version.json` and `update.json` identify the package version, MSI, hash and
pilot channel. They are release metadata; this does not add a new in-app updater.
The separate SharePoint mirror proposal in PR #1 is not part of this release.
Direct verified uploads do not imply that automatic future synchronization is
configured or functioning.
