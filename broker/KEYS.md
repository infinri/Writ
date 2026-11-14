# writ-broker key policy (owner decision D-033)

- **Purposes are separate types:** ingress (adapter seal verification), permit (permit MAC), and, from M3-07, runner authentication. A key for one purpose can never be passed where another is expected.
- **Production keys come only from the trusted key provider** (key management is M5 and M9). The broker has no default keys and loads nothing from the environment or files; callers supply keys explicitly.
- **Minimum length 32 bytes** is a structural check only; it does not establish key quality (entropy, generation, storage, rotation and revocation belong to the provider).
- **Test keys are test-only:** they exist only in `#[cfg(test)]` code and are never compiled into production builds.
- **Unknown keys fail closed:** an unregistered adapter, a removed key or a key of the wrong purpose gives `INGRESS_UNAUTHENTICATED` or `PERMIT_INVALID`.
