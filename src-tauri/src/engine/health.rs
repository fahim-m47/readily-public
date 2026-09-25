//! Is the Engine answering? One loopback request, one yes/no.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use super::status::EngineConfig;

/// Asks the Engine's `/health` route whether it is up, with the launch
/// token in the `Authorization` header.
///
/// Deliberately a hand-written loopback request rather than an HTTP client
/// crate: general-purpose clients honour `HTTP_PROXY`/`ALL_PROXY` from the
/// environment, and a health probe that can be pointed off-machine is an
/// egress path in an app whose whole claim is that it never uploads
/// (threat model, egress inventory). This request can only ever reach
/// 127.0.0.1, carries no user content, and its deadlines are the
/// supervisor's rather than a library's defaults.
pub fn probe(reach: &EngineConfig, timeout: Duration) -> bool {
    let EngineConfig { port, token } = reach;
    let address = SocketAddr::from(([127, 0, 0, 1], *port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
        return false;
    };
    if stream.set_write_timeout(Some(timeout)).is_err()
        || stream.set_read_timeout(Some(timeout)).is_err()
    {
        return false;
    }
    // The token is our own hex from `token::new_token`, so it cannot carry
    // the CRLF that would let it forge a header of its own.
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    // Only the status line matters, and "HTTP/1.1 200" is exactly its
    // first 12 bytes — nothing here parses a response body.
    let mut status = [0u8; 12];
    stream.read_exact(&mut status).is_ok() && &status == b"HTTP/1.1 200"
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::testing::{reach, TOKEN};
    use std::io::BufRead;
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    const TIMEOUT: Duration = Duration::from_millis(500);

    /// A one-shot loopback server that answers with `response`, hands the
    /// request's header lines back over the channel, and then goes away.
    fn stub_engine(response: &'static str) -> (u16, mpsc::Receiver<Vec<String>>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
        let port = listener.local_addr().expect("local addr").port();
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept");
            let mut reader = std::io::BufReader::new(stream);
            let mut lines = Vec::new();
            for line in reader.by_ref().lines().map_while(Result::ok) {
                if line.is_empty() {
                    break;
                }
                lines.push(line);
            }
            let _ = tx.send(lines);
            let _ = reader.into_inner().write_all(response.as_bytes());
        });
        (port, rx)
    }

    #[test]
    fn a_200_means_healthy() {
        let (port, _rx) = stub_engine("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");

        assert!(probe(&reach(port), TIMEOUT));
    }

    #[test]
    fn the_token_travels_in_the_authorization_header_and_nowhere_else() {
        let (port, rx) = stub_engine("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");

        probe(&reach(port), TIMEOUT);

        let request = rx.recv_timeout(TIMEOUT).expect("request observed");
        assert_eq!(request[0], "GET /health HTTP/1.1", "no token in the URL");
        assert!(
            request.contains(&format!("Authorization: Bearer {TOKEN}")),
            "token must ride the Authorization header: {request:?}"
        );
    }

    #[test]
    fn an_unauthorized_answer_is_not_healthy() {
        let (port, _rx) = stub_engine("HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n");

        assert!(!probe(&reach(port), TIMEOUT));
    }

    #[test]
    fn nothing_listening_is_not_healthy() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
        let port = listener.local_addr().expect("local addr").port();
        drop(listener);

        assert!(!probe(&reach(port), TIMEOUT));
    }

    #[test]
    fn a_silent_engine_times_out_rather_than_hanging() {
        // Accepts the connection and never answers: without a read deadline
        // the supervisor thread would block here forever.
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind loopback");
        let port = listener.local_addr().expect("local addr").port();
        thread::spawn(move || {
            let held = listener.accept();
            thread::sleep(Duration::from_secs(30));
            drop(held);
        });

        let started = std::time::Instant::now();
        assert!(!probe(&reach(port), Duration::from_millis(200)));
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
