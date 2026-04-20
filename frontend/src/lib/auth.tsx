"use client";

// Thin re-exports so existing components don't need to update their imports.
// All auth state now comes from NextAuth — no more localStorage tokens.

export { useSession as useAuth } from "next-auth/react";
export { signIn, signOut } from "next-auth/react";
