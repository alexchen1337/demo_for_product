'use client';

import React from 'react';

export default function LoginPage() {
  const handleLogin = async () => {
    const backendUrl = process.env.NEXT_PUBLIC_API_URL;
    window.location.href = `${backendUrl}/auth/cognito/login`;
  };

  return (
    <div className="login-page">
      <div className="login-container">
        <div className="login-brand">
          <h1 className="brand-name">Document Analysis</h1>
        </div>

        <div className="login-card">
          <div className="login-header">
            <h2>Welcome back</h2>
            <p>Sign in to continue to your workspace</p>
          </div>

          <button onClick={handleLogin} className="idp-login-btn">
            <svg className="idp-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" />
              <polyline points="10 17 15 12 10 7" />
              <line x1="15" y1="12" x2="3" y2="12" />
            </svg>
            <span>Continue with SSO</span>
            <svg className="arrow-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
          </button>
        </div>

        <p className="login-footer">
          Secure authentication via your organization
        </p>
      </div>
    </div>
  );
}
