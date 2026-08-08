# Implementation coverage plan

The repository implements B01 through B20 in dependency order. Pure, deterministic domain
logic is shared by API, worker, simulator and certification code. External connectors are
read-only by default, with all mutations routed through approval and Action Guard.

Local acceptance uses the deterministic simulator. Kubernetes, Slurm and EMS adapters expose
the production contract but require separate credentials and staging evidence. B20 must report
`NOT_CERTIFIED` until every mandatory external and operational gate is supplied and verified.
