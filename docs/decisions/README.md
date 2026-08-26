# Architecture Decision Records

Each ADR captures one significant decision: the context that forced it, the
choice made, and the consequences. Read the relevant ADR before changing
something these decisions rest on — several of them were reached the hard way.

Format per record: **Status · Context · Decision · Consequences**. Statuses are
*Accepted*, *Superseded by NNNN*, or *Proposed*.

| # | Decision | Status |
|---|---|---|
| [0001](0001-engine-frontend-separation.md) | Engine must not import GTK | Accepted |
| [0002](0002-token-auth-for-qobuz.md) | Session token is Qobuz's real credential | Accepted |
| [0003](0003-flatpak-for-webkit.md) | Flatpak to bundle WebKit without a user dep | Accepted |
| [0004](0004-federation-for-credential-custody.md) | Federation over hosted-holds-everything | Proposed |
