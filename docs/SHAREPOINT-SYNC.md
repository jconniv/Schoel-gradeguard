# Automatic SharePoint mirror

The GitHub Actions workflow automatically copies clean repository source after approved changes reach `main`, and it can also be run manually. It copies to:

`General/Applications/JoeJoe/Desktop Apps/Schoel GradeGuard`

It never deletes SharePoint files and excludes generated builds, local databases, machine configuration, certificates, credentials, and temporary files.

Required repository secrets:

- `SHAREPOINT_TENANT_ID`
- `SHAREPOINT_CLIENT_ID`
- `SHAREPOINT_CLIENT_SECRET`

Required repository variable:

- `SHAREPOINT_UPLOAD_PATH` = `General/Applications/JoeJoe/Desktop Apps/Schoel GradeGuard`

The destination folder must exist before the first run. Store all credential values only in GitHub Actions secrets.
