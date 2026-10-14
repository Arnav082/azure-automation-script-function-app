import azure.functions as func

import orchestrator

# Create the Function App instance.
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Register the orchestrator blueprint (timer-triggered master orchestrator).
app.register_functions(orchestrator.bp)