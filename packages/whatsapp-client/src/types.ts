export interface ChatMessage {
  phone: string;        // User's phone number (e.g., "1234567890@s.whatsapp.net")
  message: string;      // User's message text
  timestamp: Date;
}

export interface AIResponse {
  response: string;
}
