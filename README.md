# Amogh Store

A collection of apps by Amogh Krishna, available through Zeus and other clients that support AltStore sources.

Stable source: `https://amoghkrishna55.github.io/store/source.json`

Beta source: `https://amoghkrishna55.github.io/store/beta.json`

## How apps get here

Each app lives in its own repository. Pushing a version tag such as `v1.2.0` there builds the unsigned IPA and publishes it to this repository's releases, then adds the version to `apps/<app>.json`, from which the two sources above are generated. A tag such as `v1.3.0-beta.1` goes to the beta source only.

The build and publish steps are the shared workflow `.github/workflows/ios-release.yml`. An app repository calls it from a workflow of its own:

```yaml
name: Release

"on":
  push:
    tags: ["v*"]
  workflow_dispatch:
    inputs:
      tag:
        description: "Existing version tag, for example v1.2.0"
        required: true
        type: string
      publish:
        description: "Publish to the store"
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  release:
    uses: amoghkrishna55/store/.github/workflows/ios-release.yml@main
    with:
      app: myapp
      scheme: MyApp
      tag: ${{ inputs.tag || github.ref_name }}
      publish: ${{ github.event_name == 'push' || inputs.publish }}
    secrets:
      STORE_PUBLISH_TOKEN: ${{ secrets.STORE_PUBLISH_TOKEN }}
```

To add an app:

1. Add `apps/<app>.json` (copy an existing one and empty its `versions`) and a 1024×1024 icon in `assets/`.
2. Add the workflow above to the app repository. Optional inputs: `project`, `xcodegen`, `xcode`, `runner` and `test-command`.
3. Add the `STORE_PUBLISH_TOKEN` secret to the app repository: a fine-grained token limited to this repository with Contents read and write access.
