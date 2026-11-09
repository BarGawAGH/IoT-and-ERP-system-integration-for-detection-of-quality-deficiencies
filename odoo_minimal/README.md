markdown
# Odoo Community with Custom IoT Modules

Local copy of the source code of two custom Odoo Community modules, together with the configuration of a standalone installation. The repository contains no database, passwords, measurements, reports, or experimental scripts. The `security/*.csv` files define access rights and contain no measurement data.

## Modules

| Module | Scope |
|---|---|
| `iot_poc_community` | Events, mappings, API, reviews, and history |
| `iot_production_impact` | Cases, blocks, and production and material impacts |

## Installation

**Requirements:** Docker Desktop, Bash.

1. Clone or unpack the repository and start Docker Desktop.
2. In a terminal, go to the `odoo_minimal` folder and run:
   ```bash
   bash start.sh
   ```
3. Open <http://127.0.0.1:8070/web/database/manager>.
4. Read the Master Password from `.runtime/master-password.txt`.
5. Create a database named `odoo_iot_minimal` (language: English (US), without demo data).
6. In **Apps**, install **IoT Production Impact**. The **IoT Prototype** dependency is installed automatically. If the modules are not listed, enable developer mode and run **Update Apps List**.
7. Assign the **Production impact manager** role to the administrator's own account. Do not assign it to the integration account.

To stop the instance, run `bash stop.sh`. To start it again, run `bash start.sh`.

Odoo and PostgreSQL are pulled as Docker images at startup and are not included in the repository. Business data and process configuration must be entered separately.
