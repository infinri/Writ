# Third-party notices

Notices required by the licences of third-party components linked into Writ V2 production crates. Owner decision D-033 (2026-11-12) approved the BSD-3-Clause licence for `subtle` 2.6.1 specifically; its copyright notice, conditions and disclaimer are reproduced below as clause 1 and 2 require for source and binary distributions. Components under MIT or Apache-2.0 are listed in each crate's DEPENDENCIES.md and in the CI SBOM; their full notices are assembled for distribution before any release (M12).

## subtle 2.6.1 (BSD-3-Clause)

Used by `writ-broker` through `hmac` (constant-time comparison of MAC tags). Source: crates.io `subtle` 2.6.1, checksum `13c2bddecc57b384dee18652358fb23172facb8a2c51ccc10d74c157bdea3292`; licence file SHA-256 `d1fc1bc0d155df60b2e7705b6b2ae02a05c96f948e1cec6e2fb86360b09f346b`.

```text
Copyright (c) 2016-2017 Isis Agora Lovecruft, Henry de Valence. All rights reserved.
Copyright (c) 2016-2024 Isis Agora Lovecruft. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1. Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright
notice, this list of conditions and the following disclaimer in the
documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## writ-approval components (D-041 DR-2, D-042 DR-5)

Linked only by the `writ-approval` crate (not by `writ-kernel` or the standalone `writ-broker` and `writ-ledger-sqlite` closures). Each licence below is approved for the exact package and version listed only (`approval/deps-allowlist.json`); full licence texts are in [`third-party-licenses/`](third-party-licenses/) with their SHA-256. Every component is used unmodified. Checksums are the crates.io package checksums recorded in `Cargo.lock`; the CI SBOM lists the same entries.

### MPL-2.0 (D-041 DR-2)

Licence text: `third-party-licenses/MPL-2.0.txt` (from https://www.mozilla.org/media/MPL/2.0/index.txt, SHA-256 `3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04`). The packages ship no licence file of their own; their manifests declare `license = "MPL-2.0"`. Source Code Form availability: these files are covered by the Mozilla Public License 2.0 and are available unmodified from crates.io (exact versions and checksums below) and from https://github.com/kanidm/webauthn-rs (webauthn packages) and https://github.com/kanidm/crypto-glue (`crypto-glue`). Writ makes no modifications; if any is ever made, the modified files are published under MPL-2.0 with the distribution. `webauthn-authenticator-rs` is a dev-only test dependency and is never distributed.

| Package | Version | Checksum |
|---|---|---|
| `crypto-glue` | 0.2.2 | `a2199036d1ea8b42f94ec872d69dc641ec8b603ef12f9da7874cf8bfef419cb7` |
| `webauthn-attestation-ca` | 0.6.2 | `29cf6bdd3a9dba27c82ccb27df26fe7d98d7593c0c45ddddf85166a117b50301` |
| `webauthn-rs` | 0.6.2 | `13a354e6b2ef6edc62835901fc18dd413b1370d624c8cd1aa328493bd809249b` |
| `webauthn-rs-core` | 0.6.2 | `6d13e555c106095051abd70415b88546a0d3757a7990e05b4659b4fba19e46ac` |
| `webauthn-rs-proto` | 0.6.2 | `5555714098cd069e3c8b31e58ac14250b9ae185e949c21416bccb55ce8ac8a5c` |
| `webauthn-authenticator-rs` | 0.6.2 | `4bf162a8f7b5d6cd5448576d3468ae151428480a6f7d1a381ed84eaa3c8c5566` |

### Unicode-3.0, ICU4X and zerovec packages (D-041 DR-2)

Licence text: `third-party-licenses/Unicode-3.0-icu4x.txt` (the identical `LICENSE` file shipped by each package below, SHA-256 `f367c1b8e1aa262435251e442901da4607b4650e0e63a026f5044473ecfb90f2`; copyright 2020-2024 Unicode, Inc., with portions adapted from ICU4C and ICU4J). Reached through `url` and `idna`.

| Package | Version | Checksum |
|---|---|---|
| `icu_collections` | 2.4.0 | `fe090a42a0d3140fed8c63263ed1fecc02422cb2f4bfe55cd8de2de1b8e105dd` |
| `icu_locale_core` | 2.4.0 | `25d29b57c17c6528d0dc4c2141cfa2d08200465acb537a3909c8b9b8a4a984c0` |
| `icu_normalizer` | 2.4.0 | `404a3b8c486f205c87fe7f23ab6894d84a4ad76d6e4671ae1981ba7def87c599` |
| `icu_normalizer_data` | 2.4.0 | `20f0088bfb7441f7a37fa0c9e20cb1e9cba1cac6b9ce8cd2a6dc44294f8e8cbc` |
| `icu_properties` | 2.4.0 | `2ea83d991c56158f7cf898bf86436c1dfe7c07ee82fe88951f2695c9f37a379f` |
| `icu_properties_data` | 2.4.0 | `b98f29af82d02b163fe50161388350e743bb3107cc8e8c2f7314cb505faddfcf` |
| `icu_provider` | 2.4.0 | `413099315636c588ee8f011190b329b971a09a8fae9d157e01dcae8379468514` |
| `litemap` | 0.8.3 | `47d9d19d1d6efa0109d2f65ff4c85cddd50bd572e5a00127ab10987290bcefae` |
| `potential_utf` | 0.1.6 | `d83eb9bc6d8e5cf568e7a1101d60ee05e81ed50ea106026f3d18deeb046d7661` |
| `tinystr` | 0.8.5 | `abc840c238154d58f9e86b2792261edfde565a17696e1cda1a449de27979801a` |
| `writeable` | 0.6.4 | `3ad82d2a33cdc9674dc7465672f271e096168fcdbe0f799d9e6db8c5892679dc` |
| `yoke` | 0.8.4 | `417ef3fcf6c300b7dbd25c85531a746931c02ad536f3cf4786ed1e88223340b3` |
| `yoke-derive` | 0.8.4 | `ec8ebde2db3681e8c9980cc27822030e68752690ddfa9473e739aeb4dbde6d71` |
| `zerofrom` | 0.1.8 | `0ec05a11813ea801ff6d75110ad09cd0824ddba17dfe17128ea0d5f68e6c5272` |
| `zerofrom-derive` | 0.1.8 | `f75b4683f6c7f45248d4d64056a24298c6281e0993356d7d1b4a1a962ef10d4a` |
| `zerotrie` | 0.2.5 | `4ea269c3bd32f0a32c321907a2ae912ba6f4649bb0fc764a15627e99a7095a3f` |
| `zerovec` | 0.11.9 | `f58a2799cfe9a1670c7ea8edbd52ef8edb93cb37a7f83bf31ba9e35d9dae6a40` |
| `zerovec-derive` | 0.11.7 | `c3743ddbf47738d929cd1ae2e199d40d0162979dfe31aef8ae646d2bf00bf97a` |

### (MIT OR Apache-2.0) AND Unicode-3.0 (D-042 DR-5)

The Unicode-3.0 part covers Unicode data tables in these packages; the code part is used under MIT. Licence text for the Unicode part: `third-party-licenses/Unicode-3.0-unicode-ident.txt` (the identical `LICENSE-UNICODE` file shipped by both packages, SHA-256 `f7db81051789b729fea528a63ec4c938fdcb93d9d61d97dc8cc2e9df6d47f2a1`; copyright 1991-2023 Unicode, Inc.). `unicode-ident` is a build-time dependency (through `proc-macro2`); `unicode-normalization` is test-only (dev dependency of the test authenticator) and is never distributed.

| Package | Version | Checksum |
|---|---|---|
| `unicode-ident` | 1.0.27 | `a2c754d6c33795a1c324727428e5a7dedb5b06195f9890bdbcba760d3e246563` |
| `unicode-normalization` | 0.1.26 | `75cd3fb03d5288b3ba9a1ee103ac7e9cbb59f0b03d64f8ddfcc04dd42758a85d` |

### Apache-2.0 (D-041 DR-2)

`flagset` 0.4.7, checksum `b7ac824320a75a52197e8f2d787f6a38b6718bb6897a35142d749af3c0e8f4fe`. Licence text: `third-party-licenses/Apache-2.0-flagset.txt` (the package's `LICENSE` file, SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`). The package carries no NOTICE file.

### Disjunctive licences used under the MIT or Apache-2.0 option

Exact entries in `approval/deps-allowlist.json` (`licence_decision`) for packages offering MIT or Apache-2.0 among other options (for example `fiat-crypto`, `r-efi`, `wasi`, `memchr`, `zerocopy`, `tinyvec`); Writ uses them under MIT or Apache-2.0.
