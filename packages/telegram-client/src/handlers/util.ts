import { GrammyError } from 'grammy';

/**
 * The Bot API caps cloud-mode downloads at 20 MB. Oversize files surface as
 * a 400 GrammyError at `getFile` time with description `"file is too big"`.
 */
export function isFileTooBigError(error: unknown): boolean {
  return (
    error instanceof GrammyError &&
    error.error_code === 400 &&
    /file is too big/i.test(error.description)
  );
}
