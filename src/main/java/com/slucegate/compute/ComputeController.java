package main.java.com.slucegate.compute;


import org.springframework.web.bind.annotation.*;
import java.net.InetAddress;
import java.util.*;


@RestController
public class ComputeController {
    
    @GetMapping("/compute")
    public Map<String, Object> compute(@RequestParam(name = "n", defaultValue = "300000") int n) throws Exception {
        long start = System.nanoTime();
        int primes = countPrimes(n);
        long durMs = (System.nanoTime() - start) / 1_000_000;
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("hostname", InetAddress.getLocalHost().getHostName());
        res.put("n", n);
        res.put("primes", primes);
        res.put("duration_ms", durMs);
        return res;
    }


    // Simple CPU-bound workload (O(n log log n) sieve)
    private int countPrimes(int n) {
        boolean[] isPrime = new boolean[n + 1];
        Arrays.fill(isPrime, true);
        isPrime[0] = false; if (n >= 1) isPrime[1] = false;
        for (int p = 2; p * p <= n; p++) {
            if (isPrime[p]) {
                for (int k = p * p; k <= n; k += p) isPrime[k] = false;
            }
        }
        int c = 0; for (int i = 2; i <= n; i++) if (isPrime[i]) c++;
        return c;
    }
}