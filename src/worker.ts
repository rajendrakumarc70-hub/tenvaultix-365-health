// Cloudflare Worker in front of the Python app. Every request goes to one container instance that runs
// the Docker image in ./Dockerfile (FastAPI on port 8000). One instance keeps sign-in sessions and the
// SQLite database in one place.
import { Container, getContainer } from "@cloudflare/containers";

interface Env {
	HEALTH_APP: DurableObjectNamespace<HealthApp>;
	TVX_BASE_URL?: string;
	TVX_SESSION_SECRET?: string;
	TVX_CLIENT_ID?: string;
	TVX_CLIENT_SECRET?: string;
	TVX_CERT_PRIVATE_KEY?: string;
	TVX_CERT_THUMBPRINT?: string;
	TVX_CONTACT_EMAIL?: string;
	TVX_BOOKING_URL?: string;
}

// Worker variables and secrets passed through to the app. Unset ones are left out so the app's defaults apply.
const PASS_THROUGH = [
	"TVX_BASE_URL",
	"TVX_SESSION_SECRET",
	"TVX_CLIENT_ID",
	"TVX_CLIENT_SECRET",
	"TVX_CERT_PRIVATE_KEY",
	"TVX_CERT_THUMBPRINT",
	"TVX_CONTACT_EMAIL",
	"TVX_BOOKING_URL",
] as const;

export class HealthApp extends Container<Env> {
	defaultPort = 8000;
	sleepAfter = "30m"; // stop after 30 idle minutes; the next visit starts it again in a few seconds
	enableInternet = true; // Microsoft Graph, sign-in and DNS lookups

	constructor(ctx: DurableObjectState<{}>, env: Env) {
		super(ctx, env);
		const vars: Record<string, string> = {};
		for (const name of PASS_THROUGH) {
			const value = env[name];
			if (value) vars[name] = value;
		}
		this.envVars = vars;
	}
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		// Tell the app the real visitor IP and that the visit came over HTTPS.
		const headers = new Headers(request.headers);
		const ip = request.headers.get("cf-connecting-ip");
		if (ip) headers.set("x-forwarded-for", ip);
		headers.set("x-forwarded-proto", "https");
		return getContainer(env.HEALTH_APP, "main").fetch(new Request(request, { headers }));
	},
};
