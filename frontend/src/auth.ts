import NextAuth from "next-auth";
import Google from "next-auth/providers/google";
import Credentials from "next-auth/providers/credentials";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8001";

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    Google({
      clientId: process.env.AUTH_GOOGLE_ID!,
      clientSecret: process.env.AUTH_GOOGLE_SECRET!,
    }),

    Credentials({
      credentials: {
        email: {},
        password: {},
        register: {},
      },
      async authorize(credentials) {
        if (!credentials) return null;
        const email = credentials.email as string;
        const password = credentials.password as string;
        const isRegister = credentials.register === "true";

        // Registration path
        if (isRegister) {
          const regRes = await fetch(`${API_BASE}/auth/register`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
          });
          if (!regRes.ok) {
            const err = await regRes.json();
            throw new Error(err.detail ?? "Registration failed");
          }
        }

        // Login path
        const body = new URLSearchParams({ username: email, password });
        const res = await fetch(`${API_BASE}/auth/token`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body.toString(),
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail ?? "Login failed");
        }

        const data = await res.json();
        return {
          id: String(data.user_id),
          email,
          accessToken: data.access_token,
        };
      },
    }),
  ],

  callbacks: {
    jwt({ token, user, account, profile }) {
      if (user) {
        token.id = user.id;
        token.accessToken = (user as any).accessToken;
      }
      // Google sign-in: call backend to find-or-create user
      if (account?.provider === "google" && profile) {
        token.pendingGoogle = {
          google_id: profile.sub,
          email: profile.email,
          name: profile.name,
        };
      }
      return token;
    },
    async session({ session, token }) {
      session.user.id = token.id as string;

      // Resolve Google users on first session creation
      if ((token as any).pendingGoogle && !token.accessToken) {
        try {
          const res = await fetch(`${API_BASE}/auth/google`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify((token as any).pendingGoogle),
          });
          if (res.ok) {
            const data = await res.json();
            token.accessToken = data.access_token;
            token.id = String(data.user_id);
            delete (token as any).pendingGoogle;
          }
        } catch {
          // session will still work, just won't have accessToken
        }
      }

      session.accessToken = token.accessToken as string;

      // Fetch is_pro from backend on each session refresh
      if (token.accessToken) {
        try {
          const res = await fetch(`${API_BASE}/auth/me`, {
            headers: { Authorization: `Bearer ${token.accessToken}` },
          });
          if (res.ok) {
            const data = await res.json();
            token.isPro = data.is_pro ?? false;
          }
        } catch {
          token.isPro = false;
        }
      }

      session.isPro = (token.isPro as boolean) ?? false;
      return session;
    },
  },

  pages: {
    signIn: "/",
  },
});
